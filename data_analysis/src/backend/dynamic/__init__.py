import importlib

_MODULES = [
    ".dynamic_1",
]
# add all files here.
# do not add further layers of hierarchy; reload is unstable enough at this level


for m in _MODULES:
    mod = importlib.import_module(m, __name__)
    importlib.reload(mod)
