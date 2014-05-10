"""An interactive kernel that talks to frontends over 0MQ."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import print_function

import getpass
import sys
import time
import traceback
import logging
import uuid

from datetime import datetime
from signal import (
        signal, default_int_handler, SIGINT
)

import zmq
from zmq.eventloop import ioloop
from zmq.eventloop.zmqstream import ZMQStream

from IPython.config.configurable import Configurable
from IPython.core.error import StdinNotImplementedError
from IPython.core import release
from IPython.utils import py3compat
from IPython.utils.py3compat import builtin_mod, unicode_type, string_types
from IPython.utils.jsonutil import json_clean
from IPython.utils.tokenutil import token_at_cursor
from IPython.utils.traitlets import (
    Any, Instance, Float, Dict, List, Set, Integer, Unicode,
    Type, Bool,
)

from .serialize import serialize_object, unpack_apply_message
from .session import Session
from .zmqshell import ZMQInteractiveShell


#-----------------------------------------------------------------------------
# Main kernel class
#-----------------------------------------------------------------------------

protocol_version = release.kernel_protocol_version
ipython_version = release.version
language_version = sys.version.split()[0]


class Kernel(Configurable):

    #---------------------------------------------------------------------------
    # Kernel interface
    #---------------------------------------------------------------------------

    # attribute to override with a GUI
    eventloop = Any(None)
    def _eventloop_changed(self, name, old, new):
        """schedule call to eventloop from IOLoop"""
        loop = ioloop.IOLoop.instance()
        loop.add_callback(self.enter_eventloop)

    shell = Instance('IPython.core.interactiveshell.InteractiveShellABC')
    shell_class = Type(ZMQInteractiveShell)

    session = Instance(Session)
    profile_dir = Instance('IPython.core.profiledir.ProfileDir')
    shell_streams = List()
    control_stream = Instance(ZMQStream)
    iopub_socket = Instance(zmq.Socket)
    stdin_socket = Instance(zmq.Socket)
    log = Instance(logging.Logger)
    
    user_module = Any()
    def _user_module_changed(self, name, old, new):
        if self.shell is not None:
            self.shell.user_module = new
    
    user_ns = Instance(dict, args=None, allow_none=True)
    def _user_ns_changed(self, name, old, new):
        if self.shell is not None:
            self.shell.user_ns = new
            self.shell.init_user_ns()

    # identities:
    int_id = Integer(-1)
    ident = Unicode()

    def _ident_default(self):
        return unicode_type(uuid.uuid4())

    # Private interface
    
    _darwin_app_nap = Bool(True, config=True,
        help="""Whether to use appnope for compatiblity with OS X App Nap.
        
        Only affects OS X >= 10.9.
        """
    )

    # track associations with current request
    _allow_stdin = Bool(False)
    _parent_header = Dict()
    _parent_ident = Any(b'')
    # Time to sleep after flushing the stdout/err buffers in each execute
    # cycle.  While this introduces a hard limit on the minimal latency of the
    # execute cycle, it helps prevent output synchronization problems for
    # clients.
    # Units are in seconds.  The minimum zmq latency on local host is probably
    # ~150 microseconds, set this to 500us for now.  We may need to increase it
    # a little if it's not enough after more interactive testing.
    _execute_sleep = Float(0.0005, config=True)

    # Frequency of the kernel's event loop.
    # Units are in seconds, kernel subclasses for GUI toolkits may need to
    # adapt to milliseconds.
    _poll_interval = Float(0.05, config=True)

    # If the shutdown was requested over the network, we leave here the
    # necessary reply message so it can be sent by our registered atexit
    # handler.  This ensures that the reply is only sent to clients truly at
    # the end of our shutdown process (which happens after the underlying
    # IPython shell's own shutdown).
    _shutdown_message = None

    # This is a dict of port number that the kernel is listening on. It is set
    # by record_ports and used by connect_request.
    _recorded_ports = Dict()

    # A reference to the Python builtin 'raw_input' function.
    # (i.e., __builtin__.raw_input for Python 2.7, builtins.input for Python 3)
    _sys_raw_input = Any()
    _sys_eval_input = Any()

    # set of aborted msg_ids
    aborted = Set()


    def __init__(self, **kwargs):
        super(Kernel, self).__init__(**kwargs)

        # Initialize the InteractiveShell subclass
        self.shell = self.shell_class.instance(parent=self,
            profile_dir = self.profile_dir,
            user_module = self.user_module,
            user_ns     = self.user_ns,
            kernel      = self,
        )
        self.shell.displayhook.session = self.session
        self.shell.displayhook.pub_socket = self.iopub_socket
        self.shell.displayhook.topic = self._topic('execute_result')
        self.shell.display_pub.session = self.session
        self.shell.display_pub.pub_socket = self.iopub_socket
        self.shell.data_pub.session = self.session
        self.shell.data_pub.pub_socket = self.iopub_socket

        # TMP - hack while developing
        self.shell._reply_content = None

        # Build dict of handlers for message types
        msg_types = [ 'execute_request', 'complete_request',
                      'inspect_request', 'history_request',
                      'kernel_info_request',
                      'connect_request', 'shutdown_request',
                      'apply_request',
                    ]
        self.shell_handlers = {}
        for msg_type in msg_types:
            self.shell_handlers[msg_type] = getattr(self, msg_type)
        
        comm_msg_types = [ 'comm_open', 'comm_msg', 'comm_close' ]
        comm_manager = self.shell.comm_manager
        for msg_type in comm_msg_types:
            self.shell_handlers[msg_type] = getattr(comm_manager, msg_type)
        
        control_msg_types = msg_types + [ 'clear_request', 'abort_request' ]
        self.control_handlers = {}
        for msg_type in control_msg_types:
            self.control_handlers[msg_type] = getattr(self, msg_type)


    def dispatch_control(self, msg):
        """dispatch control requests"""
        idents,msg = self.session.feed_identities(msg, copy=False)
        try:
            msg = self.session.unserialize(msg, content=True, copy=False)
        except:
            self.log.error("Invalid Control Message", exc_info=True)
            return

        self.log.debug("Control received: %s", msg)

        header = msg['header']
        msg_id = header['msg_id']
        msg_type = header['msg_type']

        handler = self.control_handlers.get(msg_type, None)
        if handler is None:
            self.log.error("UNKNOWN CONTROL MESSAGE TYPE: %r", msg_type)
        else:
            try:
                handler(self.control_stream, idents, msg)
            except Exception:
                self.log.error("Exception in control handler:", exc_info=True)
    
    def dispatch_shell(self, stream, msg):
        """dispatch shell requests"""
        # flush control requests first
        if self.control_stream:
            self.control_stream.flush()
        
        idents,msg = self.session.feed_identities(msg, copy=False)
        try:
            msg = self.session.unserialize(msg, content=True, copy=False)
        except:
            self.log.error("Invalid Message", exc_info=True)
            return

        header = msg['header']
        msg_id = header['msg_id']
        msg_type = msg['header']['msg_type']
        
        # Print some info about this message and leave a '--->' marker, so it's
        # easier to trace visually the message chain when debugging.  Each
        # handler prints its message at the end.
        self.log.debug('\n*** MESSAGE TYPE:%s***', msg_type)
        self.log.debug('   Content: %s\n   --->\n   ', msg['content'])

        if msg_id in self.aborted:
            self.aborted.remove(msg_id)
            # is it safe to assume a msg_id will not be resubmitted?
            reply_type = msg_type.split('_')[0] + '_reply'
            status = {'status' : 'aborted'}
            md = {'engine' : self.ident}
            md.update(status)
            reply_msg = self.session.send(stream, reply_type, metadata=md,
                        content=status, parent=msg, ident=idents)
            return
        
        handler = self.shell_handlers.get(msg_type, None)
        if handler is None:
            self.log.error("UNKNOWN MESSAGE TYPE: %r", msg_type)
        else:
            # ensure default_int_handler during handler call
            sig = signal(SIGINT, default_int_handler)
            self.log.debug("%s: %s", msg_type, msg)
            try:
                handler(stream, idents, msg)
            except Exception:
                self.log.error("Exception in message handler:", exc_info=True)
            finally:
                signal(SIGINT, sig)
    
    def enter_eventloop(self):
        """enter eventloop"""
        self.log.info("entering eventloop %s", self.eventloop)
        for stream in self.shell_streams:
            # flush any pending replies,
            # which may be skipped by entering the eventloop
            stream.flush(zmq.POLLOUT)
        # restore default_int_handler
        signal(SIGINT, default_int_handler)
        while self.eventloop is not None:
            try:
                self.eventloop(self)
            except KeyboardInterrupt:
                # Ctrl-C shouldn't crash the kernel
                self.log.error("KeyboardInterrupt caught in kernel")
                continue
            else:
                # eventloop exited cleanly, this means we should stop (right?)
                self.eventloop = None
                break
        self.log.info("exiting eventloop")

    def start(self):
        """register dispatchers for streams"""
        self.shell.exit_now = False
        if self.control_stream:
            self.control_stream.on_recv(self.dispatch_control, copy=False)

        def make_dispatcher(stream):
            def dispatcher(msg):
                return self.dispatch_shell(stream, msg)
            return dispatcher

        for s in self.shell_streams:
            s.on_recv(make_dispatcher(s), copy=False)

        # publish idle status
        self._publish_status('starting')
    
    def do_one_iteration(self):
        """step eventloop just once"""
        if self.control_stream:
            self.control_stream.flush()
        for stream in self.shell_streams:
            # handle at most one request per iteration
            stream.flush(zmq.POLLIN, 1)
            stream.flush(zmq.POLLOUT)


    def record_ports(self, ports):
        """Record the ports that this kernel is using.

        The creator of the Kernel instance must call this methods if they
        want the :meth:`connect_request` method to return the port numbers.
        """
        self._recorded_ports = ports

    #---------------------------------------------------------------------------
    # Kernel request handlers
    #---------------------------------------------------------------------------
    
    def _make_metadata(self, other=None):
        """init metadata dict, for execute/apply_reply"""
        new_md = {
            'dependencies_met' : True,
            'engine' : self.ident,
            'started': datetime.now(),
        }
        if other:
            new_md.update(other)
        return new_md
    
    def _publish_execute_input(self, code, parent, execution_count):
        """Publish the code request on the iopub stream."""

        self.session.send(self.iopub_socket, u'execute_input',
                            {u'code':code, u'execution_count': execution_count},
                            parent=parent, ident=self._topic('execute_input')
        )
    
    def _publish_status(self, status, parent=None):
        """send status (busy/idle) on IOPub"""
        self.session.send(self.iopub_socket,
                          u'status',
                          {u'execution_state': status},
                          parent=parent,
                          ident=self._topic('status'),
                          )
    
    def _forward_input(self, allow_stdin=False):
        """Forward raw_input and getpass to the current frontend.
        
        via input_request
        """
        self._allow_stdin = allow_stdin
        
        if py3compat.PY3:
            self._sys_raw_input = builtin_mod.input
            builtin_mod.input = self.raw_input
        else:
            self._sys_raw_input = builtin_mod.raw_input
            self._sys_eval_input = builtin_mod.input
            builtin_mod.raw_input = self.raw_input
            builtin_mod.input = lambda prompt='': eval(self.raw_input(prompt))
        self._save_getpass = getpass.getpass
        getpass.getpass = self.getpass
    
    def _restore_input(self):
        """Restore raw_input, getpass"""
        if py3compat.PY3:
            builtin_mod.input = self._sys_raw_input
        else:
            builtin_mod.raw_input = self._sys_raw_input
            builtin_mod.input = self._sys_eval_input
        
        getpass.getpass = self._save_getpass
    
    def set_parent(self, ident, parent):
        """Set the current parent_header
        
        Side effects (IOPub messages) and replies are associated with
        the request that caused them via the parent_header.
        
        The parent identity is used to route input_request messages
        on the stdin channel.
        """
        self._parent_ident = ident
        self._parent_header = parent
        self.shell.set_parent(parent)
    
    def execute_request(self, stream, ident, parent):
        """handle an execute_request"""
        
        self._publish_status(u'busy', parent)
        
        try:
            content = parent[u'content']
            code = py3compat.cast_unicode_py2(content[u'code'])
            silent = content[u'silent']
            store_history = content.get(u'store_history', not silent)
        except:
            self.log.error("Got bad msg: ")
            self.log.error("%s", parent)
            return
        
        md = self._make_metadata(parent['metadata'])
        
        shell = self.shell # we'll need this a lot here
        
        self._forward_input(content.get('allow_stdin', False))
        # Set the parent message of the display hook and out streams.
        self.set_parent(ident, parent)
        
        # Re-broadcast our input for the benefit of listening clients, and
        # start computing output
        if not silent:
            self._publish_execute_input(code, parent, shell.execution_count)

        reply_content = {}
        # FIXME: the shell calls the exception handler itself.
        shell._reply_content = None
        try:
            shell.run_cell(code, store_history=store_history, silent=silent)
        except:
            status = u'error'
            # FIXME: this code right now isn't being used yet by default,
            # because the run_cell() call above directly fires off exception
            # reporting.  This code, therefore, is only active in the scenario
            # where runlines itself has an unhandled exception.  We need to
            # uniformize this, for all exception construction to come from a
            # single location in the codbase.
            etype, evalue, tb = sys.exc_info()
            tb_list = traceback.format_exception(etype, evalue, tb)
            reply_content.update(shell._showtraceback(etype, evalue, tb_list))
        else:
            status = u'ok'
        finally:
            self._restore_input()

        reply_content[u'status'] = status

        # Return the execution counter so clients can display prompts
        reply_content['execution_count'] = shell.execution_count - 1

        # FIXME - fish exception info out of shell, possibly left there by
        # runlines.  We'll need to clean up this logic later.
        if shell._reply_content is not None:
            reply_content.update(shell._reply_content)
            e_info = dict(engine_uuid=self.ident, engine_id=self.int_id, method='execute')
            reply_content['engine_info'] = e_info
            # reset after use
            shell._reply_content = None
        
        if 'traceback' in reply_content:
            self.log.info("Exception in execute request:\n%s", '\n'.join(reply_content['traceback']))
        

        # At this point, we can tell whether the main code execution succeeded
        # or not.  If it did, we proceed to evaluate user_expressions
        if reply_content['status'] == 'ok':
            reply_content[u'user_expressions'] = \
                         shell.user_expressions(content.get(u'user_expressions', {}))
        else:
            # If there was an error, don't even try to compute expressions
            reply_content[u'user_expressions'] = {}

        # Payloads should be retrieved regardless of outcome, so we can both
        # recover partial output (that could have been generated early in a
        # block, before an error) and clear the payload system always.
        reply_content[u'payload'] = shell.payload_manager.read_payload()
        # Be agressive about clearing the payload because we don't want
        # it to sit in memory until the next execute_request comes in.
        shell.payload_manager.clear_payload()

        # Flush output before sending the reply.
        sys.stdout.flush()
        sys.stderr.flush()
        # FIXME: on rare occasions, the flush doesn't seem to make it to the
        # clients... This seems to mitigate the problem, but we definitely need
        # to better understand what's going on.
        if self._execute_sleep:
            time.sleep(self._execute_sleep)

        # Send the reply.
        reply_content = json_clean(reply_content)
        
        md['status'] = reply_content['status']
        if reply_content['status'] == 'error' and \
                        reply_content['ename'] == 'UnmetDependency':
                md['dependencies_met'] = False

        reply_msg = self.session.send(stream, u'execute_reply',
                                      reply_content, parent, metadata=md,
                                      ident=ident)
        
        self.log.debug("%s", reply_msg)

        if not silent and reply_msg['content']['status'] == u'error':
            self._abort_queues()

        self._publish_status(u'idle', parent)

    def complete_request(self, stream, ident, parent):
        content = parent['content']
        code = content['code']
        cursor_pos = content['cursor_pos']
        
        txt, matches = self.shell.complete('', code, cursor_pos)
        matches = {'matches' : matches,
                   'cursor_end' : cursor_pos,
                   'cursor_start' : cursor_pos - len(txt),
                   'metadata' : {},
                   'status' : 'ok'}
        matches = json_clean(matches)
        completion_msg = self.session.send(stream, 'complete_reply',
                                           matches, parent, ident)
        self.log.debug("%s", completion_msg)

    def inspect_request(self, stream, ident, parent):
        content = parent['content']
        
        name = token_at_cursor(content['code'], content['cursor_pos'])
        info = self.shell.object_inspect(name)
        
        reply_content = {'status' : 'ok'}
        reply_content['data'] = data = {}
        reply_content['metadata'] = {}
        reply_content['found'] = info['found']
        if info['found']:
            info_text = self.shell.object_inspect_text(
                name,
                detail_level=content.get('detail_level', 0),
            )
            reply_content['data']['text/plain'] = info_text
        # Before we send this object over, we scrub it for JSON usage
        reply_content = json_clean(reply_content)
        msg = self.session.send(stream, 'inspect_reply',
                                reply_content, parent, ident)
        self.log.debug("%s", msg)

    def history_request(self, stream, ident, parent):
        # We need to pull these out, as passing **kwargs doesn't work with
        # unicode keys before Python 2.6.5.
        hist_access_type = parent['content']['hist_access_type']
        raw = parent['content']['raw']
        output = parent['content']['output']
        if hist_access_type == 'tail':
            n = parent['content']['n']
            hist = self.shell.history_manager.get_tail(n, raw=raw, output=output,
                                                            include_latest=True)

        elif hist_access_type == 'range':
            session = parent['content']['session']
            start = parent['content']['start']
            stop = parent['content']['stop']
            hist = self.shell.history_manager.get_range(session, start, stop,
                                                        raw=raw, output=output)

        elif hist_access_type == 'search':
            n = parent['content'].get('n')
            unique = parent['content'].get('unique', False)
            pattern = parent['content']['pattern']
            hist = self.shell.history_manager.search(
                pattern, raw=raw, output=output, n=n, unique=unique)

        else:
            hist = []
        hist = list(hist)
        content = {'history' : hist}
        content = json_clean(content)
        msg = self.session.send(stream, 'history_reply',
                                content, parent, ident)
        self.log.debug("Sending history reply with %i entries", len(hist))

    def connect_request(self, stream, ident, parent):
        if self._recorded_ports is not None:
            content = self._recorded_ports.copy()
        else:
            content = {}
        msg = self.session.send(stream, 'connect_reply',
                                content, parent, ident)
        self.log.debug("%s", msg)

    def kernel_info_request(self, stream, ident, parent):
        vinfo = {
            'protocol_version': protocol_version,
            'implementation': 'ipython',
            'implementation_version': ipython_version,
            'language_version': language_version,
            'language': 'python',
            'banner': self.shell.banner,
        }
        msg = self.session.send(stream, 'kernel_info_reply',
                                vinfo, parent, ident)
        self.log.debug("%s", msg)

    def shutdown_request(self, stream, ident, parent):
        self.shell.exit_now = True
        content = dict(status='ok')
        content.update(parent['content'])
        self.session.send(stream, u'shutdown_reply', content, parent, ident=ident)
        # same content, but different msg_id for broadcasting on IOPub
        self._shutdown_message = self.session.msg(u'shutdown_reply',
                                                  content, parent
        )

        self._at_shutdown()
        # call sys.exit after a short delay
        loop = ioloop.IOLoop.instance()
        loop.add_timeout(time.time()+0.1, loop.stop)

    #---------------------------------------------------------------------------
    # Engine methods
    #---------------------------------------------------------------------------

    def apply_request(self, stream, ident, parent):
        try:
            content = parent[u'content']
            bufs = parent[u'buffers']
            msg_id = parent['header']['msg_id']
        except:
            self.log.error("Got bad msg: %s", parent, exc_info=True)
            return

        self._publish_status(u'busy', parent)

        # Set the parent message of the display hook and out streams.
        shell = self.shell
        shell.set_parent(parent)

        md = self._make_metadata(parent['metadata'])
        try:
            working = shell.user_ns

            prefix = "_"+str(msg_id).replace("-","")+"_"

            f,args,kwargs = unpack_apply_message(bufs, working, copy=False)

            fname = getattr(f, '__name__', 'f')

            fname = prefix+"f"
            argname = prefix+"args"
            kwargname = prefix+"kwargs"
            resultname = prefix+"result"

            ns = { fname : f, argname : args, kwargname : kwargs , resultname : None }
            # print ns
            working.update(ns)
            code = "%s = %s(*%s,**%s)" % (resultname, fname, argname, kwargname)
            try:
                exec(code, shell.user_global_ns, shell.user_ns)
                result = working.get(resultname)
            finally:
                for key in ns:
                    working.pop(key)

            result_buf = serialize_object(result,
                buffer_threshold=self.session.buffer_threshold,
                item_threshold=self.session.item_threshold,
            )
        
        except:
            # invoke IPython traceback formatting
            shell.showtraceback()
            # FIXME - fish exception info out of shell, possibly left there by
            # run_code.  We'll need to clean up this logic later.
            reply_content = {}
            if shell._reply_content is not None:
                reply_content.update(shell._reply_content)
                e_info = dict(engine_uuid=self.ident, engine_id=self.int_id, method='apply')
                reply_content['engine_info'] = e_info
                # reset after use
                shell._reply_content = None
            
            self.session.send(self.iopub_socket, u'error', reply_content, parent=parent,
                                ident=self._topic('error'))
            self.log.info("Exception in apply request:\n%s", '\n'.join(reply_content['traceback']))
            result_buf = []

            if reply_content['ename'] == 'UnmetDependency':
                md['dependencies_met'] = False
        else:
            reply_content = {'status' : 'ok'}

        # put 'ok'/'error' status in header, for scheduler introspection:
        md['status'] = reply_content['status']

        # flush i/o
        sys.stdout.flush()
        sys.stderr.flush()
        
        reply_msg = self.session.send(stream, u'apply_reply', reply_content,
                    parent=parent, ident=ident,buffers=result_buf, metadata=md)

        self._publish_status(u'idle', parent)

    #---------------------------------------------------------------------------
    # Control messages
    #---------------------------------------------------------------------------

    def abort_request(self, stream, ident, parent):
        """abort a specifig msg by id"""
        msg_ids = parent['content'].get('msg_ids', None)
        if isinstance(msg_ids, string_types):
            msg_ids = [msg_ids]
        if not msg_ids:
            self.abort_queues()
        for mid in msg_ids:
            self.aborted.add(str(mid))

        content = dict(status='ok')
        reply_msg = self.session.send(stream, 'abort_reply', content=content,
                parent=parent, ident=ident)
        self.log.debug("%s", reply_msg)

    def clear_request(self, stream, idents, parent):
        """Clear our namespace."""
        self.shell.reset(False)
        msg = self.session.send(stream, 'clear_reply', ident=idents, parent=parent,
                content = dict(status='ok'))


    #---------------------------------------------------------------------------
    # Protected interface
    #---------------------------------------------------------------------------

    def _wrap_exception(self, method=None):
        # import here, because _wrap_exception is only used in parallel,
        # and parallel has higher min pyzmq version
        from IPython.parallel.error import wrap_exception
        e_info = dict(engine_uuid=self.ident, engine_id=self.int_id, method=method)
        content = wrap_exception(e_info)
        return content

    def _topic(self, topic):
        """prefixed topic for IOPub messages"""
        if self.int_id >= 0:
            base = "engine.%i" % self.int_id
        else:
            base = "kernel.%s" % self.ident
        
        return py3compat.cast_bytes("%s.%s" % (base, topic))
    
    def _abort_queues(self):
        for stream in self.shell_streams:
            if stream:
                self._abort_queue(stream)

    def _abort_queue(self, stream):
        poller = zmq.Poller()
        poller.register(stream.socket, zmq.POLLIN)
        while True:
            idents,msg = self.session.recv(stream, zmq.NOBLOCK, content=True)
            if msg is None:
                return

            self.log.info("Aborting:")
            self.log.info("%s", msg)
            msg_type = msg['header']['msg_type']
            reply_type = msg_type.split('_')[0] + '_reply'

            status = {'status' : 'aborted'}
            md = {'engine' : self.ident}
            md.update(status)
            reply_msg = self.session.send(stream, reply_type, metadata=md,
                        content=status, parent=msg, ident=idents)
            self.log.debug("%s", reply_msg)
            # We need to wait a bit for requests to come in. This can probably
            # be set shorter for true asynchronous clients.
            poller.poll(50)


    def _no_raw_input(self):
        """Raise StdinNotImplentedError if active frontend doesn't support
        stdin."""
        raise StdinNotImplementedError("raw_input was called, but this "
                                       "frontend does not support stdin.") 
    
    def getpass(self, prompt=''):
        """Forward getpass to frontends
        
        Raises
        ------
        StdinNotImplentedError if active frontend doesn't support stdin.
        """
        if not self._allow_stdin:
            raise StdinNotImplementedError(
                "getpass was called, but this frontend does not support input requests."
            )
        return self._input_request(prompt,
            self._parent_ident,
            self._parent_header,
            password=True,
        )
    
    def raw_input(self, prompt=''):
        """Forward raw_input to frontends
        
        Raises
        ------
        StdinNotImplentedError if active frontend doesn't support stdin.
        """
        if not self._allow_stdin:
            raise StdinNotImplementedError(
                "raw_input was called, but this frontend does not support input requests."
            )
        return self._input_request(prompt,
            self._parent_ident,
            self._parent_header,
            password=False,
        )
    
    def _input_request(self, prompt, ident, parent, password=False):
        # Flush output before making the request.
        sys.stderr.flush()
        sys.stdout.flush()
        # flush the stdin socket, to purge stale replies
        while True:
            try:
                self.stdin_socket.recv_multipart(zmq.NOBLOCK)
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    break
                else:
                    raise
        
        # Send the input request.
        content = json_clean(dict(prompt=prompt, password=password))
        self.session.send(self.stdin_socket, u'input_request', content, parent,
                          ident=ident)

        # Await a response.
        while True:
            try:
                ident, reply = self.session.recv(self.stdin_socket, 0)
            except Exception:
                self.log.warn("Invalid Message:", exc_info=True)
            except KeyboardInterrupt:
                # re-raise KeyboardInterrupt, to truncate traceback
                raise KeyboardInterrupt
            else:
                break
        try:
            value = py3compat.unicode_to_str(reply['content']['value'])
        except:
            self.log.error("Bad input_reply: %s", parent)
            value = ''
        if value == '\x04':
            # EOF
            raise EOFError
        return value

    def _at_shutdown(self):
        """Actions taken at shutdown by the kernel, called by python's atexit.
        """
        # io.rprint("Kernel at_shutdown") # dbg
        if self._shutdown_message is not None:
            self.session.send(self.iopub_socket, self._shutdown_message, ident=self._topic('shutdown'))
            self.log.debug("%s", self._shutdown_message)
        [ s.flush(zmq.POLLOUT) for s in self.shell_streams ]

