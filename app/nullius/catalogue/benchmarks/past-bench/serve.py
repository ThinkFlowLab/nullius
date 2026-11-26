"""Run one of the upstream's mock services on a port this driver picks.

The upstream starts each service as ``python mock_services/<name>/server.py``
and relies on the service to read ``PORT``. Four of the eighteen do not, so the
driver starts them through this file instead: it loads the service's own module
(the app object, its fixtures, its audit log are the upstream's, untouched) and
serves that app on the port that was free when the driver looked.

    python serve.py REPO_ROOT mock_services/notes/server.py 41001

REPO_ROOT goes on ``sys.path`` first, because each service imports
``mock_services._base`` and reads its fixtures relative to the repository.
"""
import importlib.util
import sys
from pathlib import Path


def main(argv):
    if len(argv) != 4:
        raise SystemExit("usage: serve.py REPO_ROOT MODULE_PATH PORT")
    repo, module, port = Path(argv[1]).resolve(), argv[2], int(argv[3])
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location(f"mock_service_{port}", repo / module)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {module}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    app = getattr(loaded, "app", None)
    if app is None:
        raise SystemExit(f"{module} defines no FastAPI app")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv)
