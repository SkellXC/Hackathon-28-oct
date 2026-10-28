"""QuizRoom: a Kahoot-style live quiz server (FastAPI + WebSockets).



Run:  python main.py        (serves on 0.0.0.0:8000, override with PORT=...)



All game state lives in memory in this one process, so run a single worker.

"""

from __future__ import annotations



import asyncio

import io

import json

import os

import re

import secrets

import socket

import time

from contextlib import asynccontextmanager

from pathlib import Path



import segno

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from fastapi.responses import FileResponse, Response

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field, model_validator



STATIC = Path(__file__).parent / "static"



CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O, so codes are easy to read aloud

MAX_ROOMS = 200

MAX_PLAYERS_PER_ROOM = 300

ROOM_IDLE_SECONDS = 3 * 3600  # nobody did anything for 3 hours

HOST_GONE_SECONDS = 15 * 60  # host left and never came back

ANSWER_GRACE = 0.5  # seconds of slack for network latency after the timer ends

MAX_MESSAGE_BYTES = 2000





# --------------------------------------------------------------------------- models

class QuestionIn(BaseModel):

    text: str = Field(min_length=1, max_length=200)

    options: list[str] = Field(min_length=2, max_length=4)

    correct: int = Field(ge=0)

    time: int = Field(default=20, ge=5, le=120)



    @model_validator(mode="after")

    def _check(self):

        self.text = self.text.strip()

        self.options = [o.strip()[:100] for o in self.options]

        if not self.text:

            raise ValueError("question text cannot be empty")

        if any(not o for o in self.options):

            raise ValueError("answers cannot be empty")

        if self.correct >= len(self.options):

            raise ValueError("correct answer is out of range")

        return self





class QuizIn(BaseModel):

    title: str = Field(default="Quiz", max_length=80)

    questions: list[QuestionIn] = Field(min_length=1, max_length=50)





# --------------------------------------------------------------------------- helpers

async def send(ws: WebSocket, msg: dict) -> None:

    """Send JSON to a socket; a dead or slow socket must never break the game."""

    try:

        await asyncio.wait_for(ws.send_json(msg), timeout=5)

    except Exception:

        pass





async def close_ws(ws: WebSocket, code: int = 1000) -> None:

    try:

        await ws.close(code=code)

    except Exception:

        pass





def clean_name(raw) -> str:

    if not isinstance(raw, str):

        return ""

    s = re.sub(r"[**\x00**-**\x1f\x7f**<>]", "", raw)

    return " ".join(s.split())[:20]





def lan_ip() -> str | None:

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:

        s.connect(("10.255.255.255", 1))  # no packets are actually sent

        return s.getsockname()[0]

    except OSError:

        return None

    finally:

        s.close()





# --------------------------------------------------------------------------- game state

class Player:

    def __init__(self, name: str):

        self.id = secrets.token_hex(4)

        self.token = secrets.token_urlsafe(16)  # lets a phone rejoin after a refresh

        self.name = name

        self.ws: WebSocket | None = None

        self.approved = False

        self.score = 0

        self.streak = 0

        self.last_points = 0

        self.last_correct: bool | None = None  # None means no answer





class Room:

    def __init__(self, code: str, quiz: QuizIn):

        self.code = code

        self.quiz = quiz

        self.host_token = secrets.token_urlsafe(24)

        self.host_ws: WebSocket | None = None

        self.players: dict[str, Player] = {}

        self.state = "lobby"  # lobby | question | reveal | ended

        self.qi = -1

        self.q_started = 0.0

        self.answers: dict[str, tuple[int, float]] = {}  # player id -> (choice, seconds taken)

        self.counts: list[int] = []

        self.locked = False

        self.timer: asyncio.Task | None = None

        self.touched = time.monotonic()

        self.host_gone_since: float | None = None



    # ---- views -----------------------------------------------------------

    def approved(self) -> list[Player]:

        return [p for p in self.players.values() if p.approved]



    def ranking(self) -> list[Player]:

        return sorted(self.approved(), key=lambda p: (-p.score, p.name.lower()))



    def rank_of(self, p: Player) -> int:

        return 1 + sum(1 for x in self.approved() if x.score > p.score)



    def remaining_ms(self) -> int:

        q = self.quiz.questions[self.qi]

        return max(0, int((q.time - (time.monotonic() - self.q_started)) * 1000))



    def expected(self) -> int:

        return sum(1 for p in self.approved() if p.ws)



    def question_view(self) -> dict:

        q = self.quiz.questions[self.qi]

        return {"text": q.text, "options": q.options, "time": q.time}



    def host_view(self) -> dict:

        v = {

            "type": "sync",

            "code": self.code,

            "title": self.quiz.title,

            "state": self.state,

            "locked": self.locked,

            "qi": self.qi,

            "total": len(self.quiz.questions),

            "players": [

                {"id": p.id, "name": p.name, "score": p.score, "online": p.ws is not None}

                for p in self.approved()

            ],

            "pending": [{"id": p.id, "name": p.name} for p in self.players.values() if not p.approved],

            "answered": len(self.answers),

            "expected": self.expected(),

        }

        if self.state in ("question", "reveal"):

            v["question"] = self.question_view()

            v["remaining_ms"] = self.remaining_ms() if self.state == "question" else 0

        if self.state == "reveal":

            v["correct"] = self.quiz.questions[self.qi].correct

            v["counts"] = self.counts

        if self.state in ("reveal", "ended"):

            limit = 20 if self.state == "ended" else 5

            v["leaderboard"] = [

                {"name": p.name, "score": p.score, "delta": p.last_points} for p in self.ranking()[:limit]

            ]

        return v



    def player_view(self, p: Player) -> dict:

        v = {

            "type": "sync",

            "status": "approved" if p.approved else "pending",

            "name": p.name,

            "state": self.state,

            "qi": self.qi,

            "total": len(self.quiz.questions),

            "score": p.score,

        }

        if not p.approved:

            return v

        if self.state in ("question", "reveal"):

            v["question"] = self.question_view()

            v["remaining_ms"] = self.remaining_ms() if self.state == "question" else 0

            v["answered"] = p.id in self.answers

            if p.id in self.answers:

                v["choice"] = self.answers[p.id][0]

        if self.state == "reveal":

            v["correct"] = self.quiz.questions[self.qi].correct

            v["result"] = {"correct": p.last_correct, "points": p.last_points, "streak": p.streak}

        if self.state in ("reveal", "ended"):

            v["rank"] = self.rank_of(p)

            v["of"] = len(self.approved())

        if self.state == "ended":

            v["podium"] = [{"name": x.name, "score": x.score} for x in self.ranking()[:3]]

        return v



    # ---- sending ---------------------------------------------------------

    async def send_host(self) -> None:

        if self.host_ws:

            await send(self.host_ws, self.host_view())



    async def send_player(self, p: Player) -> None:

        if p.ws:

            await send(p.ws, self.player_view(p))



    async def broadcast(self) -> None:

        jobs = [self.send_host()] + [self.send_player(p) for p in self.players.values()]

        await asyncio.gather(*jobs)



    async def send_progress(self) -> None:

        if self.host_ws:

            await send(self.host_ws, {"type": "progress", "answered": len(self.answers), "expected": self.expected()})



    # ---- players ---------------------------------------------------------

    async def join(self, ws: WebSocket, msg: dict) -> Player | None:

        token = msg.get("token")

        if isinstance(token, str) and token:

            for p in self.players.values():

                if secrets.compare_digest(p.token, token):

                    if p.ws and p.ws is not ws:

                        await close_ws(p.ws)

                    p.ws = ws

                    await send(ws, {"type": "joined", "token": p.token})

                    await self.send_player(p)

                    await self.send_host()

                    return p

        name = clean_name(msg.get("name"))

        if not name:

            code = "expired" if token else "bad_name"

            text = "Your session ended. Enter your name to join again." if token else "Enter a name to join."

            await send(ws, {"type": "error", "code": code, "message": text})

            return None

        if self.locked:

            await send(ws, {"type": "error", "code": "locked", "message": "The host isn't taking new players."})

            return None

        if len(self.players) >= MAX_PLAYERS_PER_ROOM:

            await send(ws, {"type": "error", "code": "full", "message": "This room is full."})

            return None

        if any(p.name.lower() == name.lower() for p in self.players.values()):

            await send(ws, {"type": "error", "code": "name_taken", "message": "That name is taken. Try another."})

            return None

        p = Player(name)

        p.ws = ws

        self.players[p.id] = p

        await send(ws, {"type": "joined", "token": p.token})

        await self.send_player(p)

        await self.send_host()

        return p



    async def remove_player(self, p: Player, reason: str) -> None:

        self.players.pop(p.id, None)

        self.answers.pop(p.id, None)

        ws, p.ws = p.ws, None

        if ws:

            await send(ws, {"type": "removed", "reason": reason})

            await close_ws(ws)

        await self.send_host()

        await self.maybe_finish_question()



    # ---- game flow -------------------------------------------------------

    def _cancel_timer(self) -> None:

        if self.timer and self.timer is not asyncio.current_task():

            self.timer.cancel()

        self.timer = None



    async def _timer(self, qi: int, seconds: int) -> None:

        await asyncio.sleep(seconds + ANSWER_GRACE + 0.1)

        if self.state == "question" and self.qi == qi:

            await self.end_question()



    async def start_question(self, qi: int) -> None:

        self._cancel_timer()

        self.qi = qi

        self.state = "question"

        self.answers = {}

        self.counts = []

        self.q_started = time.monotonic()

        for p in self.players.values():

            p.last_correct = None

            p.last_points = 0

        self.timer = asyncio.create_task(self._timer(qi, self.quiz.questions[qi].time))

        await self.broadcast()



    async def handle_answer(self, p: Player, choice) -> None:

        if self.state != "question" or not p.approved or p.id in self.answers:

            return

        q = self.quiz.questions[self.qi]

        if not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice < len(q.options):

            return

        elapsed = time.monotonic() - self.q_started

        if elapsed > q.time + ANSWER_GRACE:

            return

        self.answers[p.id] = (choice, elapsed)

        await self.send_player(p)

        if not await self.maybe_finish_question():

            await self.send_progress()



    async def maybe_finish_question(self) -> bool:

        """End the question early once every connected player has answered."""

        if self.state != "question":

            return False

        online = [p for p in self.approved() if p.ws]

        if online and all(p.id in self.answers for p in online):

            await self.end_question()

            return True

        return False



    async def end_question(self) -> None:

        if self.state != "question":

            return

        self._cancel_timer()

        q = self.quiz.questions[self.qi]

        counts = [0] * len(q.options)

        for p in self.approved():

            if p.id not in self.answers:

                p.streak = 0

                p.last_correct = None

                p.last_points = 0

                continue

            choice, elapsed = self.answers[p.id]

            counts[choice] += 1

            if choice == q.correct:

                p.streak += 1

                base = round(1000 * (1 - min(elapsed / q.time, 1.0) / 2))  # 1000 instant, 500 at the buzzer

                bonus = min((p.streak - 1) * 100, 500)

                p.last_points = base + bonus

                p.score += p.last_points

                p.last_correct = True

            else:

                p.streak = 0

                p.last_points = 0

                p.last_correct = False

        self.counts = counts

        self.state = "reveal"

        await self.broadcast()



    async def advance(self) -> None:

        if self.state == "lobby":

            if self.approved():

                await self.start_question(0)

        elif self.state == "question":

            await self.end_question()

        elif self.state == "reveal":

            if self.qi + 1 < len(self.quiz.questions):

                await self.start_question(self.qi + 1)

            else:

                self.state = "ended"

                await self.broadcast()



    async def host_action(self, msg: dict) -> None:

        t = msg.get("type")

        pid = msg.get("id")

        p = self.players.get(pid) if isinstance(pid, str) else None

        if t == "approve" and p and not p.approved:

            p.approved = True

            await self.send_player(p)

            await self.send_host()

        elif t == "approve_all":

            for x in self.players.values():

                x.approved = True

            await self.broadcast()

        elif t == "deny" and p and not p.approved:

            await self.remove_player(p, "denied")

        elif t == "kick" and p:

            await self.remove_player(p, "kicked")

        elif t == "lock":

            self.locked = bool(msg.get("value"))

            await self.send_host()

        elif t in ("start", "next"):

            await self.advance()

        elif t == "end":

            await self.close("closed")



    async def close(self, reason: str) -> None:

        self._cancel_timer()

        ROOMS.pop(self.code, None)

        for p in list(self.players.values()):

            if p.ws:

                await send(p.ws, {"type": "removed", "reason": reason})

                await close_ws(p.ws)

                p.ws = None

        if self.host_ws:

            await close_ws(self.host_ws)

            self.host_ws = None





ROOMS: dict[str, Room] = {}





def new_code() -> str:

    for _ in range(100):

        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))

        if code not in ROOMS:

            return code

    raise HTTPException(503, "Could not allocate a room code")





async def janitor() -> None:

    while True:

        await asyncio.sleep(60)

        now = time.monotonic()

        for room in list(ROOMS.values()):

            gone = room.host_gone_since is not None and now - room.host_gone_since > HOST_GONE_SECONDS

            if gone or now - room.touched > ROOM_IDLE_SECONDS:

                await room.close("expired")





@asynccontextmanager

async def lifespan(app: FastAPI):

    task = asyncio.create_task(janitor())

    yield

    task.cancel()





app = FastAPI(title="QuizRoom", lifespan=lifespan)





# --------------------------------------------------------------------------- HTTP

@app.post("/api/rooms")

async def create_room(quiz: QuizIn):

    if len(ROOMS) >= MAX_ROOMS:

        raise HTTPException(503, "The server is busy. Try again in a few minutes.")

    room = Room(new_code(), quiz)

    room.host_gone_since = time.monotonic()  # cleaned up if the host never connects

    ROOMS[room.code] = room

    return {"code": room.code, "host_token": room.host_token}





@app.get("/api/rooms/{code}")

async def room_status(code: str):

    room = ROOMS.get(code.upper())

    return {"exists": room is not None, "locked": bool(room and room.locked)}





@app.get("/api/health")
async def health():
    return {"ok": True, "service": "QuizRoom"}


@app.get("/api/info")

async def info():

    return {"lan_ip": lan_ip()}





@app.get("/api/qr")

async def qr(data: str = Query(min_length=1, max_length=300)):

    code = segno.make(data, error="m")

    buf = io.BytesIO()

    code.save(buf, kind="svg", border=2, dark="#16213E", light="#FFFFFF", omitsize=True, xmldecl=False)

    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})





@app.get("/")

async def page_index():

    return FileResponse(STATIC / "index.html")





@app.get("/host")

async def page_host():

    return FileResponse(STATIC / "host.html")





@app.get("/play")

async def page_play():

    return FileResponse(STATIC / "play.html")





app.mount("/static", StaticFiles(directory=STATIC), name="static")





# --------------------------------------------------------------------------- WebSockets

async def read_json(ws: WebSocket) -> dict | None:

    raw = await ws.receive_text()

    if len(raw) > MAX_MESSAGE_BYTES:

        return None

    try:

        msg = json.loads(raw)

    except ValueError:

        return None

    return msg if isinstance(msg, dict) else None





@app.websocket("/ws/host/{code}")

async def ws_host(ws: WebSocket, code: str, token: str = Query("")):

    await ws.accept()

    room = ROOMS.get(code.upper())

    if not room:

        await send(ws, {"type": "error", "code": "no_room", "message": "That room has closed."})

        await close_ws(ws, 4404)

        return

    if not secrets.compare_digest(room.host_token, token):

        await send(ws, {"type": "error", "code": "forbidden", "message": "Not the host of this room."})

        await close_ws(ws, 4403)

        return

    if room.host_ws:

        await close_ws(room.host_ws)  # a newer host tab replaces the old one

    room.host_ws = ws

    room.host_gone_since = None

    await room.send_host()

    try:

        while True:

            msg = await read_json(ws)

            if msg is None:

                continue

            room.touched = time.monotonic()

            await room.host_action(msg)

    except (WebSocketDisconnect, RuntimeError):

        pass

    finally:

        if room.host_ws is ws:

            room.host_ws = None

            room.host_gone_since = time.monotonic()





@app.websocket("/ws/play/{code}")

async def ws_play(ws: WebSocket, code: str):

    await ws.accept()

    room = ROOMS.get(code.upper())

    if not room:

        await send(ws, {"type": "error", "code": "no_room", "message": "That room doesn't exist. Check the code."})

        await close_ws(ws, 4404)

        return

    player: Player | None = None

    try:

        while True:

            msg = await read_json(ws)

            if msg is None:

                continue

            room.touched = time.monotonic()

            kind = msg.get("type")

            if player is None:

                if kind == "join":

                    player = await room.join(ws, msg)

            elif kind == "answer":

                await room.handle_answer(player, msg.get("choice"))

            elif kind == "leave":

                await room.remove_player(player, "left")

                return

    except (WebSocketDisconnect, RuntimeError):

        pass

    finally:

        if player and player.id in room.players and player.ws is ws:

            player.ws = None

            await room.send_host()

            await room.maybe_finish_question()





if __name__ == "__main__":

    import uvicorn



    port = int(os.environ.get("PORT", "8000"))

    ip = lan_ip()

    print(f"\n  Host a quiz:  http://localhost:{port}/host")

    if ip:

        print(f"  Phones join:  http://{ip}:{port}/   (same Wi-Fi)\n")

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")