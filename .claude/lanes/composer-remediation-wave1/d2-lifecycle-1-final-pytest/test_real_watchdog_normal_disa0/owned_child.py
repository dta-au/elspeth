import asyncio, threading
from elspeth.web.process_watchdog import create_process_watchdog, ProcessCompletionWitness
async def main():
    control=create_process_watchdog(threading.Event())
    control.assert_watching()
    print("WATCHING",flush=True)
    await control.complete(ProcessCompletionWitness(control.target))
    print("JOINED",flush=True)
asyncio.run(main())
