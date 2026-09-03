import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.responses import JSONResponse
from kubernetes import client as k8s_client, config as k8s_config
from urllib.parse import parse_qs
from dependencies import verify_slack_signature
import commands


@asynccontextmanager
async def lifespan(app: FastAPI):
    http = None
    try:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()  # local fallback for dev/testing
        http = httpx.Client(timeout=30.0)
        commands.init_clients(k8s_client, http)
        task = asyncio.create_task(commands.kafka_restart_monitor())
        try:
            yield
        finally:
            task.cancel()
    finally:
        if http is not None:
            http.close()


app = FastAPI(lifespan=lifespan)


@app.post("/slack/command")
async def handle_command(
    background_tasks: BackgroundTasks,
    body: bytes = Depends(verify_slack_signature),
) -> JSONResponse:
    form = parse_qs(body.decode())
    text = form.get("text", [""])[0].strip()
    resp_url = form.get("response_url", [""])[0]
    channel = form.get("channel_id", [""])[0]

    parts = text.split()
    subcmd = parts[0] if parts else "help"
    args = parts[1:]

    background_tasks.add_task(commands.run_command, subcmd, args, resp_url, channel)
    return JSONResponse({"response_type": "ephemeral", "text": f"Running: `{text}`..."})
