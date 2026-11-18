"""Concrete signals. Like studies, a signal sees data only through the point-in-time door.

``tests/test_architecture.py`` holds every module here to the same rules as
``finresearch.studies``: no imports from the rest of the data layer, the data
sources, the run framework or the registry, and no files, processes or network.
"""
