"""Python script Exporter class"""

#-----------------------------------------------------------------------------
# Copyright (c) 2013, the IPython Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

from IPython.utils.traitlets import Unicode

from .templateexporter import TemplateExporter

#-----------------------------------------------------------------------------
# Classes
#-----------------------------------------------------------------------------

class PythonExporter(TemplateExporter):
    """
    Exports a Python code file.
    """
    
    file_extension = Unicode(
        'py', config=True, 
        help="Extension of the file that should be written to disk")

    output_mimetype = 'text/x-python'
