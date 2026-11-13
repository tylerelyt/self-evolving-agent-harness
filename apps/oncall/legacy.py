"""Execute compatibility entrypoints from their single maintained source."""
from pathlib import Path
import sys


def execute(namespace, relative):
    from apps.oncall.paths import APP, prepare
    prepare()
    source = APP / relative
    namespace["__file__"] = str(source)
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)
