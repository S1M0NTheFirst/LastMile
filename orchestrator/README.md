# LastMile orchestrator

This is the first LLM-to-robot vertical slice. It exposes a Gemini-backed
`POST /chat` endpoint and a direct `POST /tasks` endpoint. Gemini currently has
one tool: `assign_move_task`.

## Run on Machine B

From `LastMile/`:

```bash
python3 -m venv orchestrator/.venv
orchestrator/.venv/bin/pip install -r orchestrator/requirements.txt

export REGISTRY_URL="http://<machine-b-ip>:3000/api/registry"
export GEMINI_API_KEY="<your-key>"
export GEMINI_MODEL="gemini-3.8-flash"
export ROBOT_AUTH_TOKEN="a-shared-local-secret"

orchestrator/.venv/bin/uvicorn orchestrator.app:app --host 0.0.0.0 --port 8001
```

The Gemini key remains in the orchestrator process and is never sent to a
browser or robot.

## Start robots

Set the same token in the shell before running `join.py` on Machine A and
Machine B:

```bash
export ROBOT_AUTH_TOKEN="a-shared-local-secret"
python3 join.py --registry <machine-b-ip>:3000 --count 2
```

## Test without Gemini

```bash
curl -X POST http://localhost:8001/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"move_to","payload":{"x":50,"y":50}}'
```

## Test with Gemini

```bash
curl -X POST http://localhost:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Move an available robot to position 50,50"}'
```
