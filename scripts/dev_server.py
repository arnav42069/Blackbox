"""Reload factory: the app keeps serving Vite's watched production output on one origin."""
from blackbox.app import create_app

def factory():
    app=create_app()
    print('BlackBox dev pairing (one use): http://127.0.0.1:8765/#pair='+app.state.auth.pairing,flush=True)
    return app
