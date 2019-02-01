'''
This module contains integration tests.

These tests rely on a RethinkDB server running on localhost 28015.
'''

# Add this project to the Python path:
from os.path import dirname
from sys import path
path.append(dirname(dirname(__file__)))