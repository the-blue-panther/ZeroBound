import asyncio
import websockets

async def main():
    try:
        async with websockets.connect('ws://localhost:8001/ws') as ws:
            print('CONNECTED TO WS')
            for _ in range(3):
                msg = await ws.recv()
                print('RECEIVED:', msg[:150])
    except Exception as e:
        print('ERROR:', type(e).__name__, e)

asyncio.run(main())
