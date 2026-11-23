"""Start SWE-agent v1.1.0 through its public configuration and environment APIs.

Preparation calls its ToolHandler.install without creating a model. Execution
uses its default configuration and RunSingle, including its normal tool setup.
"""
import os
import importlib.util
from pathlib import Path
import sys
import subprocess
import json


def check_tool_python():
    """Refuse an incompatible task interpreter without replacing its environment."""
    result = subprocess.run(
        ["python", "-c", "import json, sys; print(json.dumps(list(sys.version_info[:3])))"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    version = json.loads(result.stdout)
    if tuple(version) < (3, 8):
        raise RuntimeError(
            "SWE-agent's default editor requires tree-sitter==0.21.3 (Python >=3.8); "
            "the task PATH selects Python " + ".".join(map(str, version)) + ". "
            "Nullius will not replace the benchmark's Python environment."
        )


def main():
    check_tool_python()
    os.environ["PIP_CONSTRAINT"] = str(Path(__file__).with_name("tools.constraints.txt"))
    # The pinned checkout is mounted read-only and owned by the host user.
    # Permit GitPython's provenance lookup for this one path, process-locally.
    source = Path(importlib.util.find_spec("sweagent").origin).parent.parent
    index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ.update({"GIT_CONFIG_COUNT": str(index + 1),
                       f"GIT_CONFIG_KEY_{index}": "safe.directory",
                       f"GIT_CONFIG_VALUE_{index}": str(source)})
    import yaml
    from sweagent import REPO_ROOT
    from sweagent.environment.swe_env import EnvironmentConfig, SWEEnv
    from sweagent.tools.tools import ToolConfig, ToolHandler

    config = yaml.safe_load((REPO_ROOT / 'config/default.yaml').read_text())
    # SWE-ReX intentionally starts shells with a fresh environment. Use the
    # upstream tool environment option for pip constraints and preparation's
    # temporary proxy; normal execution receives no proxy variables.
    values = config['agent']['tools'].setdefault('env_variables', {})
    for name in ('PIP_CONSTRAINT', 'PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'no_proxy'):
        if name in os.environ:
            values[name] = os.environ[name]
    environment = {'deployment': {'type': 'local'}}
    if sys.argv[1] == 'prepare':
        env = SWEEnv.from_config(EnvironmentConfig.model_validate(environment))
        try:
            env.start()
            ToolHandler(ToolConfig.model_validate(config['agent']['tools'])).install(env)
        finally:
            env.close()
        return
    from sweagent.run.run_single import RunSingle, RunSingleConfig
    environment['repo'] = {'type': 'preexisting', 'repo_name': str(Path.cwd()).lstrip('/')}
    config['env'] = environment
    config['agent']['model'] = {'name': os.environ['NULLIUS_SWE_MODEL'],
                                'api_base': os.environ['NULLIUS_SWE_BASE'],
                                'api_key': os.environ['NULLIUS_SWE_KEY']}
    config['problem_statement'] = {'type': 'text', 'text': sys.argv[2]}
    config['output_dir'] = str(Path.home() / 'swe-agent-output')
    RunSingle.from_config(RunSingleConfig.model_validate(config)).run()


if __name__ == '__main__':
    main()
