"""The session's stdio MCP server ``computer`` for the kit: the computer port's pipe (``computer_pipe.py``, two
folders up), run from the kit's folder, where a benchmark's server command names its files.

    pipe.py SOCKET
"""
import os
import runpy
import sys

PIPE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "computer_pipe.py")

if __name__ == "__main__":
    sys.argv[0] = PIPE
    runpy.run_path(PIPE, run_name="__main__")
