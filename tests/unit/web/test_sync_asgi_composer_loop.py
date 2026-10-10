import asyncio

from fastapi import FastAPI, Response

from tests.unit.web._sync_asgi_client import SyncASGITestClient


def test_one_loop_keeps_request_owned_tasks_alive_and_preserves_cookies() -> None:
    app = FastAPI()
    loops = []
    tasks = []
    completed = []

    @app.post("/submit")
    async def submit(response: Response):
        loops.append(asyncio.get_running_loop())
        response.set_cookie("custody", "owned")

        async def work():
            await asyncio.sleep(0)
            completed.append(asyncio.get_running_loop())

        tasks.append(asyncio.create_task(work()))
        return {"accepted": True}

    @app.get("/poll")
    async def poll():
        loops.append(asyncio.get_running_loop())
        return {"completed": bool(completed)}

    client = SyncASGITestClient(app)

    async def drive(http):
        assert (await http.post("/submit")).status_code == 200
        await tasks[0]
        assert (await http.get("/poll")).json() == {"completed": True}

    client.run_in_one_loop(drive)
    assert loops[0] is loops[1] is completed[0]
    assert client.cookies["custody"] == "owned"


def test_split_loop_cancels_task_before_a_later_poll_can_drive_it() -> None:
    app = FastAPI()
    tasks = []
    loops = []

    @app.post("/submit")
    async def submit():
        loops.append(asyncio.get_running_loop())
        tasks.append(asyncio.create_task(asyncio.Event().wait()))
        return {"accepted": True}

    @app.get("/poll")
    async def poll():
        loops.append(asyncio.get_running_loop())
        return {"cancelled": tasks[0].cancelled()}

    client = SyncASGITestClient(app)
    assert client.post("/submit").status_code == 200
    assert client.get("/poll").json() == {"cancelled": True}
    assert loops[0] is not loops[1]
