import asyncio, threading, time
from elspeth.web.process_watchdog import create_process_watchdog
from elspeth.web.process_watchdog_codec import RecoveryReason
async def main():
    control=create_process_watchdog(threading.Event())
    ack=await control.begin_recovery(RecoveryReason.REQUIRED_GENERATION_UNRESOLVED)
    print("ACK",flush=True)
    thread=threading.Thread(target=lambda:threading.Event().wait(),daemon=False)
    thread.start()
    while True: time.sleep(1)
asyncio.run(main())
