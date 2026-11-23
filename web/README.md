# DFT-Agent Bench web interface

The local web application browses tasks, previews prompts, manages evaluation runs and displays reports. It reads the sibling `bench/` directory and registers the two solvers from `agent/`. Browsing needs neither a model key nor a Quantum ESPRESSO installation.

From the repository root, use a Python 3.10 or 3.11 environment:

```sh
python3 -m venv web/.venv
. web/.venv/bin/activate
python -m pip install -r bench/requirements.txt -r web/backend/requirements.txt
cd web
export AUTH_TOKEN="$(python -c 'from getpass import getpass; print(getpass("Paste a random login token from your password manager: "))')"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765 --no-proxy-headers
```

Open [the local interface](http://127.0.0.1:8765). Enter the same token in the login screen. This single-token configuration is for one trusted local operator. The login token is kept in the current browser tab. Prompt enhancement and failure analysis send the selected prompt or diagnostic excerpts to the configured model provider when invoked. The browser loads pinned React and Babel scripts from a CDN. The default user can see the 245 stable and 30 exploratory tasks. The authenticated catalogue lists 275 tasks; the frozen preset contains the 88 visible members of the 100-task slice. The full manifest remains available in `bench/`. A manifest entry with `is_stable=false` is hidden from ordinary users.

Live evaluations additionally need the [benchmark environment](../bench/README.md), the chosen [agent dependencies and model credentials](../agent/README.md), and working QE binaries for the selected CPU/GPU mode. Set `QE_CPU_WRAPPER_DIR` for CPU or `QE_GPU_WRAPPER_DIR` for GPU to the directory containing the corresponding executables before starting the server. Local run logs and the SQLite database are created under `web/runs_local/`. The server serves static files only from `web/frontend/`.

Adapted from the collaborators' `Pluto235/dft-agent-bench-web-fresh` snapshot `aa2926875383d1ed29eb4e2e5475ef7b4e59d3b1`. This distribution excludes operator accounts, tokens, configuration files, databases, historical transcripts and tunnel/deployment scripts.
