"""Terminal-Bench 2.0's driver for the Nullius standard (JSON lines on stdin and stdout).

Each task is a folder of the upstream at its pinned commit: ``instruction.md``
(the prompt), ``task.toml`` (its limits), ``environment/`` (its own
Dockerfile, its own FROM: the task's container), ``tests/`` (``test.sh``,
which writes ``/logs/verifier/reward.txt``) and ``solution/solve.sh`` (the
authors' reference solution).

units  - the dataset's tasks: a few that build fast (``small``), or every
         task whose reference solution downloads nothing (``offline``), or all 89 (``full``)
setup  - the task's instruction as the prompt; its own image (``image``:
         by the run's image option, the prebuilt image its task.toml names,
         pulled by digest, or its environment folder built as published - a
         task whose build no longer works is pulled either way - with the
         memory and CPUs it asks for), its own agent timeout
         (``agent_seconds``, task.toml's agent.timeout_sec), its preparation
         (``prepare``: tmux installed when the image has none, as Terminal-Bench's
         own harness gives every task) and its check (``verify``: tests/ copied to
         /tests and test.sh run in the task's container once the agent has
         ended and what it left running is stopped - kept, for the few tasks
         whose tests talk to a service it was asked to leave running - with
         the way out it downloads its test runner over, its results
         collected from /logs/verifier). The tests reach the container only
         then; nothing is written into the workspace.
score  - the reward test.sh wrote, read from what the check collected
oracle - the reference control: solution/ copied to /solution and solve.sh
         run in the task's container, as the agent would have worked

The driver never runs docker: the run does each step in the unit's container.
"""
import json
import os
import sys
import tomllib

# Fast to build, their reference solutions fetching nothing: the first ones build in seconds.
SMALL = ["cancel-async-tasks", "regex-log", "sqlite-db-truncate", "fix-git", "polyglot-c-py",
         "git-leak-recovery", "sanitize-git-repo", "extract-elf"]

# Every task whose solve.sh fetches nothing (no package manager, no download): the agent works with
# no way out but its model on these tasks.
OFFLINE = [
    "bn-fit-modify", "break-filter-js-from-html", "cancel-async-tasks", "circuit-fibsqrt", "code-from-image",
    "constraints-scheduling", "custom-memory-heap-crash", "db-wal-recovery", "distribution-search", "extract-elf",
    "extract-moves-from-video", "feal-differential-cryptanalysis", "feal-linear-cryptanalysis", "filter-js-from-html",
    "fix-code-vulnerability", "fix-git", "fix-ocaml-gc", "git-leak-recovery", "git-multibranch", "gpt2-codegolf",
    "headless-terminal", "large-scale-text-editing", "llm-inference-batching-scheduler", "mailman",
    "make-mips-interpreter", "model-extraction-relu-logits", "modernize-scientific-stack", "mteb-retrieve",
    "multi-source-data-merger", "openssl-selfsigned-cert", "password-recovery", "path-tracing", "path-tracing-reverse",
    "polyglot-c-py", "polyglot-rust-c", "portfolio-optimization", "prove-plus-comm", "pytorch-model-recovery",
    "qemu-alpine-ssh", "qemu-startup", "query-optimize", "regex-chess", "regex-log", "reshard-c4-data",
    "sanitize-git-repo", "schemelike-metacircular-eval", "sqlite-db-truncate", "torch-pipeline-parallelism",
    "torch-tensor-parallelism", "tune-mjcf", "video-processing", "vulnerable-secret", "write-compressor",
]
# Tasks whose solutions need the internet at the pinned upstream. The benchmark grants it,
# through the public-address proxy, to the agent and reference solution alike.
INTERNET = [
    "adaptive-rejection-sampler", "build-cython-ext", "build-pmars", "build-pov-ray",
    "caffe-cifar-10", "chess-best-move", "cobol-modernization", "compile-compcert",
    "configure-git-webserver", "count-dataset-tokens", "crack-7z-hash", "dna-assembly",
    "dna-insert", "financial-document-processor", "gcode-to-text", "hf-model-inference",
    "install-windows-3.11", "kv-store-grpc", "largest-eigenval", "log-summary-date-ranges",
    "make-doom-for-mips", "mcmc-sampling-stan", "merge-diff-arc-agi-task", "mteb-leaderboard",
    "nginx-request-logging", "overfull-hbox", "protein-assembly", "pypi-server",
    "pytorch-model-cli", "raman-fitting", "rstan-to-pystan", "sam-cell-seg",
    "sparql-university", "sqlite-with-gcov", "train-fasttext", "winning-avg-corewars",
]
ALL = sorted(OFFLINE + INTERNET)
DATASETS = {"small": SMALL, "offline": OFFLINE, "full": ALL}

# The prebuilt image each task's task.toml names (environment.docker_image, the one Harbor runs unless told
# to build), by the digest its tag had when pinned here (Docker Hub, 2026-10-14).
IMAGES = {
    "bn-fit-modify": "docker.io/alexgshaw/bn-fit-modify@sha256:5aeb11ca6e802b83816c9a31f055ac9d028309a853509400c2e3139ac2d8380d",
    "break-filter-js-from-html": "docker.io/alexgshaw/break-filter-js-from-html@sha256:d7bfb45e47cf10d2e21dc520239215498f4eba9c1ab2d0cd7d647985dddb4fa8",
    "cancel-async-tasks": "docker.io/alexgshaw/cancel-async-tasks@sha256:84c7fae6b256dcc56a350790e2a9715eefc7dad662a9d8e8a472363aa71ef18d",
    "circuit-fibsqrt": "docker.io/alexgshaw/circuit-fibsqrt@sha256:29783439f529eaed2145592f15af7a2281161528860392a382c825489030ae3a",
    "code-from-image": "docker.io/alexgshaw/code-from-image@sha256:2be9cdb746ed18c1b2b1404032efae0b2235a1b8fc479ef17730c95409d89f76",
    "constraints-scheduling": "docker.io/alexgshaw/constraints-scheduling@sha256:567ce5a189f8d11ac461790876e934cc7af38391baf89a78f95f4dafc1fec3b0",
    "custom-memory-heap-crash": "docker.io/alexgshaw/custom-memory-heap-crash@sha256:1895d7d6240d5fb2ec774d6bfdd1057d170f8d199c15c582e4585d3c63802557",
    "db-wal-recovery": "docker.io/alexgshaw/db-wal-recovery@sha256:0e33ea5ec823975d1bd6c3778395c9f94251dd88f571146057bff6adb7e4594e",
    "distribution-search": "docker.io/alexgshaw/distribution-search@sha256:1448f7aa1c6080488251d00350e8e90477858989b1bec0accc866d7b3220738a",
    "extract-elf": "docker.io/alexgshaw/extract-elf@sha256:6932e4cb318464307eacd497ef8dc617eaf551b6a90231f815ec0b911895cfed",
    "extract-moves-from-video": "docker.io/alexgshaw/extract-moves-from-video@sha256:14a9dcca7d08ce53c5a0ed9fbd663a996ef28f207c199ed69d9ccf6064f8b184",
    "feal-differential-cryptanalysis": "docker.io/alexgshaw/feal-differential-cryptanalysis@sha256:bea93bafe9eab601ca58729f5735a13f8339f52055322d1cf840e3742b54a287",
    "feal-linear-cryptanalysis": "docker.io/alexgshaw/feal-linear-cryptanalysis@sha256:ea78749a8422c6228d26c8753f45906a6a2df089906764b7e3c3e27e8f718ee7",
    "filter-js-from-html": "docker.io/alexgshaw/filter-js-from-html@sha256:92acda0f124b988036a6f426ce0bc47fac19f5efe9fc5e6ea3ea52ccb075d0a4",
    "fix-code-vulnerability": "docker.io/alexgshaw/fix-code-vulnerability@sha256:cac325252991f823713b2d0441502972901dd782bd67f66c03d9b1e410dac5c0",
    "fix-git": "docker.io/alexgshaw/fix-git@sha256:61e431c00c58df652287aadce5457634d9f9330cfdd153ebdf2802df0d540119",
    "fix-ocaml-gc": "docker.io/alexgshaw/fix-ocaml-gc@sha256:66a1e2293076c9ef27a118611b3089a6284fdf467fd4675592d0cce7d11e5b0b",
    "git-leak-recovery": "docker.io/alexgshaw/git-leak-recovery@sha256:62160e522a4238b8bcd522411ea97b5b200f811e5899d34947e66809bef7e586",
    "git-multibranch": "docker.io/alexgshaw/git-multibranch@sha256:5bc449921c138295f6ddd8dca2977319fee2ce523811dbe56cec4ca663385fb5",
    "gpt2-codegolf": "docker.io/alexgshaw/gpt2-codegolf@sha256:537e7bd26b02db5c761230515fa09dc05f89403c6737fbdae4b5793ebc2c7066",
    "headless-terminal": "docker.io/alexgshaw/headless-terminal@sha256:eb7e209672bf6cef2785fafd9e13509b10626c327bcc2b37f5bf40ca83eaf3aa",
    "large-scale-text-editing": "docker.io/alexgshaw/large-scale-text-editing@sha256:719adca3f1388220546ce6a155eee56eff3c4fe318183100320606a210f6b59c",
    "llm-inference-batching-scheduler": "docker.io/alexgshaw/llm-inference-batching-scheduler@sha256:19e79aa49be4e55dccca1dde966515ebe6852314b6010bcb9230afb48b996774",
    "mailman": "docker.io/alexgshaw/mailman@sha256:04fac73db7e2b4f49d43f09fbc2266a94d70131f07efa1393d370abc059f4094",
    "make-mips-interpreter": "docker.io/alexgshaw/make-mips-interpreter@sha256:082fc8821b317f30fdfbf8d08d528874ce331c03e8083aef0406d48cdd7132a2",
    "model-extraction-relu-logits": "docker.io/alexgshaw/model-extraction-relu-logits@sha256:52fe1f089f38650f0dc22d7531ee6e01ebd526de1b349aaa2ecb331dca9fabce",
    "modernize-scientific-stack": "docker.io/alexgshaw/modernize-scientific-stack@sha256:64e69cee13bf6b0b9016e735b51891bce996a60f1e2d1a005bf12c949e221d71",
    "mteb-retrieve": "docker.io/alexgshaw/mteb-retrieve@sha256:99dd676a21fd3124b7b8164cd93e8f0c51f1fdab2f84a831c61049eff78d063d",
    "multi-source-data-merger": "docker.io/alexgshaw/multi-source-data-merger@sha256:8b32782078ff7383a1b4e5d3cecca8ce287e30f50bb4c0e5db009a18064e666e",
    "openssl-selfsigned-cert": "docker.io/alexgshaw/openssl-selfsigned-cert@sha256:4c948a4e630af2435ae0a19108fc0814a946ac2fa29a512469e0fc77b38c8c12",
    "password-recovery": "docker.io/alexgshaw/password-recovery@sha256:15fcd84bf3b06270ee148211cc6da6599b13ec2f41ac1d0894cf75c2225872ee",
    "path-tracing": "docker.io/alexgshaw/path-tracing@sha256:4b44725102cd627dfa895191a5a30d16dae3c5623bb43da6459f79135db6b482",
    "path-tracing-reverse": "docker.io/alexgshaw/path-tracing-reverse@sha256:7911e42479d8f6d8af1248bc52442a0efbc5a33e3ecf535fdc20d252a2c64142",
    "polyglot-c-py": "docker.io/alexgshaw/polyglot-c-py@sha256:0f1c3b7816d70cf5551573fd6aeef76893f2ae3000be2419997b6871b5d987ed",
    "polyglot-rust-c": "docker.io/alexgshaw/polyglot-rust-c@sha256:3ba84f0e138a4b65e58c53f920a0fc852571e6402901c3bdbb876e9dab6e6c5e",
    "portfolio-optimization": "docker.io/alexgshaw/portfolio-optimization@sha256:93461db3cec697c31585941a3b9387c1398495aa1ff0aee2ac752c7230b50ef8",
    "prove-plus-comm": "docker.io/alexgshaw/prove-plus-comm@sha256:e26741d01681a9da1beeff8b7b7b65fe2b921b8b122a306a3707a81ebd051f73",
    "pytorch-model-recovery": "docker.io/alexgshaw/pytorch-model-recovery@sha256:83efba8d1a163dfe62694789e271ed55010509b6deabbf2d7d6472b40ca8d69a",
    "qemu-alpine-ssh": "docker.io/alexgshaw/qemu-alpine-ssh@sha256:541e8214a8c4faa7b556531b899d45da21048fa2ca6dd328d8cb6c4cffa8e964",
    "qemu-startup": "docker.io/alexgshaw/qemu-startup@sha256:1a666e049db553c5e713830b1f5de1953210ab7c99212d006bb4e46cbc6b6d04",
    "query-optimize": "docker.io/alexgshaw/query-optimize@sha256:28de7da87004eab5b29bd5eaf1823579bc5b73ccea4b80349a5623185770da14",
    "regex-chess": "docker.io/alexgshaw/regex-chess@sha256:e3b6d61d2d3930fbbdc5f7bf091a7f8b7f6125e1951ef8a7dbe165b1ef62942c",
    "regex-log": "docker.io/alexgshaw/regex-log@sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9",
    "reshard-c4-data": "docker.io/alexgshaw/reshard-c4-data@sha256:92d209e704785e17ad6b3f740e813dd72bc7ac33982ce435d197ab9d890eac2a",
    "sanitize-git-repo": "docker.io/alexgshaw/sanitize-git-repo@sha256:4b5234da5bb0d67f3b0bf8db40a2883c07a5219f62b64c2bf9ff1ac84cd0f672",
    "schemelike-metacircular-eval": "docker.io/alexgshaw/schemelike-metacircular-eval@sha256:b485be38bf80d56c1a4cd9da95bbf7ec129dc03f6786b86c0525518a96f6d1db",
    "sqlite-db-truncate": "docker.io/alexgshaw/sqlite-db-truncate@sha256:aabac93c93bd1f310e6a6fb893911d7735026ed18491c72133c9196a09092ca4",
    "torch-pipeline-parallelism": "docker.io/alexgshaw/torch-pipeline-parallelism@sha256:3cb7b39d86f1704b20e668851da0b08b34b3270c01d583f136d65190984a8d1b",
    "torch-tensor-parallelism": "docker.io/alexgshaw/torch-tensor-parallelism@sha256:c50ac169e11465f41b49749fe87e4487f24d31a0c39c0906cd3242cc5499c690",
    "tune-mjcf": "docker.io/alexgshaw/tune-mjcf@sha256:56c197739db2db79562384581fcc9d3d71e6c171bf052104b1a7ee0282e7c3cf",
    "video-processing": "docker.io/alexgshaw/video-processing@sha256:a2c0f39e3ab04e67ac6e49cad9167bb0d987babe32511768c01aa0c6905a875f",
    "vulnerable-secret": "docker.io/alexgshaw/vulnerable-secret@sha256:61ebb40454dd103aa2f7e71ad6dafd91cf2b301e6bb07e69d5b472412d1ee15b",
    "write-compressor": "docker.io/alexgshaw/write-compressor@sha256:3618e1f8a997b437c09cd3dbaac736705809c05f6f12f584b65722175970ebe1",
    # The additional internet tasks: task.toml tags resolved on 2026-11-12.
    "adaptive-rejection-sampler": "docker.io/alexgshaw/adaptive-rejection-sampler@sha256:985bed39e8448bc95f138b5f766b59003a95c4be171c96af8e76448338f3dd8f",
    "build-cython-ext": "docker.io/alexgshaw/build-cython-ext@sha256:3612a38fadb89a96f74a1a951fb0b0af734198fd160571eeaba6401593234594",
    "build-pmars": "docker.io/alexgshaw/build-pmars@sha256:57a82706f7491e0b2d1ff7e927e97ae5339892dd09c47ac43290b4ba1f5abc6f",
    "build-pov-ray": "docker.io/alexgshaw/build-pov-ray@sha256:874baf49341e2853a15a9184e308c7eb632e97f434359fcff2891c6e5ca1b39b",
    "caffe-cifar-10": "docker.io/alexgshaw/caffe-cifar-10@sha256:5b54d6e8f1b392cdeb819e28215853f500b72b8e08b91270c7a3693bf44d1fe2",
    "chess-best-move": "docker.io/alexgshaw/chess-best-move@sha256:bb447f94d9e2a8ed879f85c85a514b213b7418f9fe11fd7b2428a0b0e436e647",
    "cobol-modernization": "docker.io/alexgshaw/cobol-modernization@sha256:593ab9df3d83f771e927888b2d9436b380cdf7ec4d0af2a9f0802d541edebba0",
    "compile-compcert": "docker.io/alexgshaw/compile-compcert@sha256:f316947b7be23ad0d187320126eb1cbb5685f48342c6543fdd3ac839aa9e1a7f",
    "configure-git-webserver": "docker.io/alexgshaw/configure-git-webserver@sha256:9e48389b917fd4650dda1f64406bd7198bbf20dc1c6b0e5c4d81a3c16369f88c",
    "count-dataset-tokens": "docker.io/alexgshaw/count-dataset-tokens@sha256:9c513c4ba342bd95be501f6f5eadd4806a2b18749c87c251615825100130232d",
    "crack-7z-hash": "docker.io/alexgshaw/crack-7z-hash@sha256:0f4453abd774c5a3d3d7e66ba28fae88ec2e49ada3a993b324ebc16c348666d3",
    "dna-assembly": "docker.io/alexgshaw/dna-assembly@sha256:d1adf6835f1dd91205ba70e452c699d0aea601010038e5617f370716efb50569",
    "dna-insert": "docker.io/alexgshaw/dna-insert@sha256:4dd8760694e355de85ecf03605cb487ccdb33322f6afad513d2920829baa33b2",
    "financial-document-processor": "docker.io/alexgshaw/financial-document-processor@sha256:ef6c9cfaaf14cdd200163008a188d5baa9f626f3bfb8d3e7d6f30c08518e6251",
    "gcode-to-text": "docker.io/alexgshaw/gcode-to-text@sha256:0979ef40c6a3e8c4e7ab5b6c2524c74625a84799e15cd45dd98dcf83b11efd4d",
    "hf-model-inference": "docker.io/alexgshaw/hf-model-inference@sha256:f96dd7d3c85c7257b404fd63ba8d24ced2478cd90232c20e297c372f953fa607",
    "install-windows-3.11": "docker.io/alexgshaw/install-windows-3.11@sha256:c6695097316ef8810abce0545e347e65354aef9d51d4f7f662c3242e742f6a3e",
    "kv-store-grpc": "docker.io/alexgshaw/kv-store-grpc@sha256:3399400800dcb207634daa42bc1b052e831e285cc9d221eea66c47bc0fc79791",
    "largest-eigenval": "docker.io/alexgshaw/largest-eigenval@sha256:cb9e3e3b09d0783f3bbcfeec8d77bc32d3791204fb2be6650b19be4fbc082b2e",
    "log-summary-date-ranges": "docker.io/alexgshaw/log-summary-date-ranges@sha256:cbeb6ba905c2fec294f16cd5e16e3ea7f2e04d38ac2484d51a11de262aa7dc51",
    "make-doom-for-mips": "docker.io/alexgshaw/make-doom-for-mips@sha256:80a9459f1b94b692ea8346f68e6b864f6c4d984fbdf6ffd68ad332308300c2ec",
    "mcmc-sampling-stan": "docker.io/alexgshaw/mcmc-sampling-stan@sha256:073fc36a3347b4f6b43bd6e98339542c41ed531c771af8bee40ba10f00cfabaf",
    "merge-diff-arc-agi-task": "docker.io/alexgshaw/merge-diff-arc-agi-task@sha256:bfc2a235f2ceea64ffb1ffc5529c2acebf570bff61f3668f0e5669ef570c17d4",
    "mteb-leaderboard": "docker.io/alexgshaw/mteb-leaderboard@sha256:968de9456bfc53728f7df0cb6c9ce33e17bd0e5563ddfe031e6ed2964965aecf",
    "nginx-request-logging": "docker.io/alexgshaw/nginx-request-logging@sha256:693e64431a0dbf45952e8b91a298d54fd60247e501c6623fb8fa3ebc07fc3d3d",
    "overfull-hbox": "docker.io/alexgshaw/overfull-hbox@sha256:67fe0ccc0ff2305d6fcb3652977d377b901bb7bd96904b6765835935574992e1",
    "protein-assembly": "docker.io/alexgshaw/protein-assembly@sha256:94de701b57e7ecfdbada6044f8534876e4b58874f8f313184dadc4d7533e0818",
    "pypi-server": "docker.io/alexgshaw/pypi-server@sha256:d18bb30f47c7dcaa3acdbc31ac7413c98eadca2ea16540bbd380f2f063b95276",
    "pytorch-model-cli": "docker.io/alexgshaw/pytorch-model-cli@sha256:c996884b5fb5fb2c0c07d67da2c4f974f271f07234ed28e1a868240375c57a67",
    "raman-fitting": "docker.io/alexgshaw/raman-fitting@sha256:401fa95d5d491f44a473eda9d2c52930f9565cc889c796eabcb6e20fec26852e",
    "rstan-to-pystan": "docker.io/alexgshaw/rstan-to-pystan@sha256:b23d48839408524326095c15ada4145c772b87646cf299022ff1f58c97baba91",
    "sam-cell-seg": "docker.io/alexgshaw/sam-cell-seg@sha256:d76bdeef19d8113f4094013249c982eca93e62e46130484846269b2d378879d0",
    "sparql-university": "docker.io/alexgshaw/sparql-university@sha256:92fa4304e51167f9fb4466144eba33ef9272a7df0ac1cab9fc3986bccd1ab708",
    "sqlite-with-gcov": "docker.io/alexgshaw/sqlite-with-gcov@sha256:f986820b00c74ce75db7288719d364df313fde7c7917bea6eb6352a08431a89f",
    "train-fasttext": "docker.io/alexgshaw/train-fasttext@sha256:3c77da9617e7fa577de04aef73fb1ac3114be32d7ffef72609c83929d842b157",
    "winning-avg-corewars": "docker.io/alexgshaw/winning-avg-corewars@sha256:ad7a6b1c53aed59e10916c692aa7e2ae00042a4283187430aee1e3e79f72d5c6",
}
# Tasks whose Dockerfile no longer builds as published (it clones a repository that is gone): pulled whatever
# the run's image option says.
PULL = {"fix-git"}
# Where each task's container comes from: the run's ``image`` option (version 2) - pull, its prebuilt image,
# or build, from its environment/Dockerfile as published. A run without the option (version 1) builds.
IMAGE = os.environ.get("NULLIUS_OPTION_IMAGE", "build")
LOGS = "/logs/verifier"

# Tasks whose tests talk to a service the agent was asked to start and leave running (a mail server, an
# SSH or telnet login into a VM, a web server behind a push hook): what the agent left running is kept
# for their check. Every other task's check runs with nothing of the agent's still running.
KEEP_PROCESSES = {"git-multibranch", "mailman", "qemu-alpine-ssh", "qemu-startup",
                  "configure-git-webserver", "kv-store-grpc", "nginx-request-logging", "pypi-server"}


# Terminal-Bench's own harness gives every task's container tmux (its reference agent works in a tmux
# session, and git-multibranch's solution starts one). Most images have none: the unit's container is
# prepared with it, as root, through the check's proxy, before anyone works in it; an image that has it
# is left alone.
TMUX = r"""
command -v tmux >/dev/null 2>&1 && { echo "tmux is already here: $(command -v tmux)"; exit 0; }
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq --no-install-recommends tmux
elif command -v apk >/dev/null 2>&1; then apk add --no-cache tmux
elif command -v dnf >/dev/null 2>&1; then dnf install -y tmux
elif command -v microdnf >/dev/null 2>&1; then microdnf install -y tmux
elif command -v yum >/dev/null 2>&1; then yum install -y tmux
elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install tmux
elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm tmux
else echo "no package manager to install tmux with"; exit 1
fi
command -v tmux || { echo "tmux was not installed"; exit 1; }
"""
PREPARE = {"run": ["sh", "-c", TMUX], "seconds": 600, "network": True}


def task(unit):
    if unit not in ALL:
        raise ValueError(f"{unit} is not a task of this benchmark's datasets")
    here = os.path.join(os.environ["NULLIUS_UPSTREAM"], unit)
    with open(os.path.join(here, "task.toml"), "rb") as f:
        return here, tomllib.load(f)


def units(dataset):
    return {"units": list(DATASETS[dataset])}


def setup(unit, dir):
    here, t = task(unit)
    with open(os.path.join(here, "instruction.md")) as f:
        prompt = f.read().strip()
    env = t.get("environment") or {}
    image = {"pull": IMAGES[unit]} if IMAGE == "pull" or unit in PULL else {"build": f"{unit}/environment"}
    if env.get("memory_mb"):
        image["memory_mb"] = env["memory_mb"]
    if env.get("cpus"):
        image["cpus"] = env["cpus"]
    # the task's own agent timeout from version 2 on; version 1 ran every task at the run's turn_seconds
    agent = (t.get("agent") or {}).get("timeout_sec") if os.environ.get("NULLIUS_VERSION", "1") != "1" else None
    return {"prompt": prompt, "environment": "task", "image": image,
            **({"internet": True} if unit in INTERNET else {}),
            **({"agent_seconds": agent} if agent else {}),                # the task's own agent timeout, not the run's
            "prepare": PREPARE,
            "verify": {"copy": {f"{unit}/tests": "/tests"}, "run": ["bash", "/tests/test.sh"], "collect": LOGS,
                       "seconds": (t.get("verifier") or {}).get("timeout_sec") or 900, "network": True,
                       **({"keep_processes": True} if unit in KEEP_PROCESSES else {})}}


def score(unit, dir, transcript, answer, verified=None):
    if not verified:
        return {"reward": 0.0, "passed": False, "detail": {"why": "the task's check did not run"}}
    detail = {"exit": verified.get("exit"), "timed_out": verified.get("timed_out"),
              "output": (verified.get("output") or "")[-3000:]}
    try:
        with open(os.path.join(verified["dir"], "reward.txt")) as f:
            reward = float(f.read().strip() or 0)
    except (OSError, ValueError):
        detail["why"] = "test.sh wrote no reward"
        reward = 0.0
    try:
        with open(os.path.join(verified["dir"], "ctrf.json")) as f:
            detail["tests"] = (json.load(f).get("results") or {}).get("summary")
    except (OSError, ValueError):
        pass
    return {"reward": 1.0 if reward >= 1.0 else 0.0, "passed": reward >= 1.0, "detail": detail}


def oracle(unit, dir):
    here, t = task(unit)
    return {"answer": "", "solve": {"copy": {f"{unit}/solution": "/solution"}, "run": ["bash", "/solution/solve.sh"],
                                    "seconds": (t.get("agent") or {}).get("timeout_sec") or 900}}


HANDLERS = {"units": units, "setup": setup, "score": score, "oracle": oracle}
for line in sys.stdin:
    try:
        req = json.loads(line)
        out = HANDLERS[req.pop("op")](**req)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out), flush=True)
