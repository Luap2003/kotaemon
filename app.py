# server.py

import os
import uvicorn
from fastapi import FastAPI
from theflow.settings import settings as flowsettings
import gradio as gr
# 1) Your FastAPI sub-app for file uploads
from ktem.api.fastapi_file_upload import app as file_api_app

# 2) Your Gradio application
from ktem.main import App as KtemApp

# ——— Set up any env vars like GRADIO_TEMP_DIR ———
KH_APP_DATA_DIR = getattr(flowsettings, "KH_APP_DATA_DIR", ".")
GRADIO_TEMP_DIR = os.getenv("GRADIO_TEMP_DIR") or os.path.join(KH_APP_DATA_DIR, "gradio_tmp")
os.environ["GRADIO_TEMP_DIR"] = GRADIO_TEMP_DIR

# ——— Build the FastAPI “parent” app ———
app = FastAPI()

# Mount your FastAPI file-upload service at /dokumentation
app.mount("/dokumentation", file_api_app, name="file_upload_api")

# ——— Build the Gradio UI ———
ktem = KtemApp()
demo: gr.Blocks = ktem.make()
demo.queue()  # if you need it

# Mount the Gradio Blocks into this FastAPI app at the root (or "/ui", etc.)
# This wraps all Gradio routes under the chosen path
app = gr.mount_gradio_app(app, demo, path="/")
# ——— Run everything via Uvicorn ———
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
