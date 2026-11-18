"""Rocket-League-style car soccer: blue vs orange, high-frequency telemetry.

This is the "large, intensive game" from the design meeting: besides stat
events (goals, shots, saves, assists, demos) it streams ``tick`` snapshots
with every car's position and boost (0-100), the ball position, the score and
the match clock. Ties at full time go to sudden-death overtime.

Field coordinates follow Rocket League units: x in [-4096, 4096],
y in [-5120, 5120] (blue defends -y), z in [0, 2044].
"""

from __future__ import annotations

from ..models import EventType, GameplayEvent, PlayerInfo, PlayerState, TeamInfo, TickPayload, Vec3
from .base import GameEngine

POINTS = {"goal": 100, "assist": 50, "save": 50, "shot": 20, "demo": 25}
FIELD_X, FIELD_Y, CEILING = 4096.0, 5120.0, 2044.0
BLUE, ORANGE = "blue", "orange"


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class RocketEngine(GameEngine):
    def _setup(self) -> None:
        size = self.spec.team_size
        self.teams = [TeamInfo(team_id=BLUE, name="Blue"), TeamInfo(team_id=ORANGE, name="Orange")]
        self.team_scores = {BLUE: 0, ORANGE: 0}
        names = self.make_names(size * 2)
        self.players = []
        for i in range(size * 2):
            team = BLUE if i < size else ORANGE
            self.players.append(
                PlayerInfo(
                    player_id=f"m{self.match_id}p{i + 1:02d}",
                    display_name=names[i],
                    team_id=team,
                )
            )
        self.ids = [p.player_id for p in self.players]
        self.team_of = {p.player_id: p.team_id for p in self.players}
        self.roster = {t: [p for p in self.ids if self.team_of[p] == t] for t in (BLUE, ORANGE)}
        self.skill = {pid: self.rng.lognormvariate(0.0, 0.3) for pid in self.ids}
        self.points = {pid: 0 for pid in self.ids}
        self.goals = {pid: 0 for pid in self.ids}
        self.boost = {pid: 33 for pid in self.ids}
        self.momentum = BLUE if self.rng.random() < 0.5 else ORANGE
        self._kickoff()

    # ---- state helpers --------------------------------------------------------
    @staticmethod
    def _other(team: str) -> str:
        return ORANGE if team == BLUE else BLUE

    @staticmethod
    def _attack_dir(team: str) -> float:
        return 1.0 if team == BLUE else -1.0  # blue attacks +y

    def _kickoff(self) -> None:
        self.ball = [0.0, 0.0, 92.75]
        self.ball_v = [0.0, 0.0, 0.0]
        self.pos: dict[str, list[float]] = {}
        for team in (BLUE, ORANGE):
            d = -self._attack_dir(team)  # own half
            for k, pid in enumerate(self.roster[team]):
                self.pos[pid] = [(k - (len(self.roster[team]) - 1) / 2) * 2048.0, d * (2560.0 + 512 * k), 17.0]
                self.boost[pid] = 33

    def _team_strength(self, team: str) -> float:
        return sum(self.skill[p] for p in self.roster[team]) / len(self.roster[team])

    def _ev(self, type: EventType, player: str, **kw) -> GameplayEvent:
        pts = POINTS[type.value]
        self.points[player] += pts
        return self.emit(type, player_id=player, team_id=self.team_of[player], points=pts, **kw)

    # ---- game loop ------------------------------------------------------------
    def on_action(self) -> list[GameplayEvent]:
        # attacking side: biased by current momentum and team strength
        w_m = 1.3
        sb, so = self._team_strength(BLUE), self._team_strength(ORANGE)
        wb = sb * (w_m if self.momentum == BLUE else 1.0)
        wo = so * (w_m if self.momentum == ORANGE else 1.0)
        attack = self.rng.choices([BLUE, ORANGE], weights=[wb, wo])[0]
        defend = self._other(attack)

        if self.stat_enabled("demos") and self.rng.random() < 0.2:
            return self._demo(attack, defend)
        return self._shot(attack, defend)

    def _demo(self, attack: str, defend: str) -> list[GameplayEvent]:
        attacker = self.rng.choices(self.roster[attack], weights=[self.skill[p] for p in self.roster[attack]])[0]
        victim = self.rng.choice(self.roster[defend])
        # victim respawns in own goal with 33 boost
        self.pos[victim] = [0.0, -self._attack_dir(defend) * FIELD_Y * 0.9, 17.0]
        self.boost[victim] = 33
        self.boost[attacker] = max(0, self.boost[attacker] - 20)
        return [self._ev(EventType.DEMO, attacker, target_player_id=victim)]

    def _shot(self, attack: str, defend: str) -> list[GameplayEvent]:
        out: list[GameplayEvent] = []
        shooter = self.rng.choices(self.roster[attack], weights=[self.skill[p] for p in self.roster[attack]])[0]
        d = self._attack_dir(attack)
        self.ball = [self.rng.uniform(-1500, 1500), d * self.rng.uniform(3000, 4800), self.rng.uniform(93, 900)]
        self.ball_v = [self.rng.uniform(-300, 300), d * self.rng.uniform(1500, 3000), self.rng.uniform(0, 500)]
        out.append(self._ev(EventType.SHOT, shooter))

        p_goal = _clamp(0.22 * self.skill[shooter] / self._team_strength(defend), 0.08, 0.6)
        if self.rng.random() < p_goal:
            self.goals[shooter] += 1
            self.team_scores[attack] += 1
            out.append(self._ev(EventType.GOAL, shooter, team_score_delta=1))
            mates = [p for p in self.roster[attack] if p != shooter]
            if self.stat_enabled("assists") and mates and self.rng.random() < 0.6:
                assister = self.rng.choice(mates)
                out.append(self._ev(EventType.ASSIST, assister, target_player_id=shooter))
            self.momentum = defend  # conceding team kicks off
            self._kickoff()
            if self.overtime:
                out += self.finish("overtime_goal", winner_team_id=attack, winner_player_id=self._mvp(attack))
            return out

        if self.stat_enabled("saves") and self.rng.random() < 0.6:
            keeper = self.rng.choices(self.roster[defend], weights=[self.skill[p] for p in self.roster[defend]])[0]
            out.append(self._ev(EventType.SAVE, keeper, target_player_id=shooter))
            self.momentum = defend
            self.ball_v = [self.ball_v[0], -self.ball_v[1] * 0.6, self.ball_v[2]]
        return out

    def on_tick(self) -> list[GameplayEvent]:
        dt = 1.0 / self.spec.tick_hz
        # ball: integrate velocity with drag, gravity and wall bounces
        bx, by, bz = self.ball
        vx, vy, vz = self.ball_v
        vx += self.rng.gauss(0, 120)
        vy += self.rng.gauss(0, 120) + 150 * self._attack_dir(self.momentum)
        vz -= 650 * dt
        vx, vy = vx * 0.98, vy * 0.98
        bx, by, bz = bx + vx * dt, by + vy * dt, bz + vz * dt
        if abs(bx) > FIELD_X:
            bx, vx = _clamp(bx, -FIELD_X, FIELD_X), -vx * 0.7
        if abs(by) > FIELD_Y:
            by, vy = _clamp(by, -FIELD_Y, FIELD_Y), -vy * 0.7
        if bz < 92.75:
            bz, vz = 92.75, abs(vz) * 0.6
        if bz > CEILING:
            bz, vz = CEILING, -abs(vz)
        self.ball, self.ball_v = [bx, by, bz], [vx, vy, vz]

        states = []
        for pid in self.ids:
            x, y, z = self.pos[pid]
            boosting = self.boost[pid] > 0 and self.rng.random() < 0.3
            speed = (2300.0 if boosting else 1400.0) * dt
            dx, dy = bx - x, by - y
            dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
            x += dx / dist * speed * self.rng.uniform(0.3, 1.0) + self.rng.gauss(0, 60)
            y += dy / dist * speed * self.rng.uniform(0.3, 1.0) + self.rng.gauss(0, 60)
            z = 17.0 if self.rng.random() < 0.85 else self.rng.uniform(17, 600)
            self.pos[pid] = [_clamp(x, -FIELD_X, FIELD_X), _clamp(y, -FIELD_Y, FIELD_Y), z]
            if boosting:
                self.boost[pid] = max(0, self.boost[pid] - 3)
            else:
                r = self.rng.random()
                if r < 0.01:
                    self.boost[pid] = 100  # big pad
                elif r < 0.06:
                    self.boost[pid] = min(100, self.boost[pid] + 12)  # small pad
            px, py, pz = self.pos[pid]
            states.append(
                PlayerState(
                    player_id=pid,
                    pos=Vec3(x=round(px, 1), y=round(py, 1), z=round(pz, 1)),
                    boost=self.boost[pid],
                )
            )
        payload = TickPayload(
            time_remaining_s=self.time_remaining(),
            overtime=self.overtime,
            team_scores=dict(self.team_scores),
            ball=Vec3(x=round(bx, 1), y=round(by, 1), z=round(bz, 1)),
            players=states,
        )
        return [self.emit(EventType.TICK, payload=payload)]

    def _mvp(self, team: str | None = None) -> str:
        pool = self.roster[team] if team else self.ids
        return max(pool, key=lambda p: (self.points[p], self.goals[p], p))

    def winners(self) -> tuple[str | None, str | None]:
        b, o = self.team_scores[BLUE], self.team_scores[ORANGE]
        if b == o:
            return None, self._mvp()
        team = BLUE if b > o else ORANGE
        return team, self._mvp(team)

    def on_regulation_end(self) -> list[GameplayEvent]:
        if self.team_scores[BLUE] == self.team_scores[ORANGE] and self.spec.max_overtime_s > 0:
            self.overtime = True
            return []
        team, player = self.winners()
        return self.finish("time_expired", winner_team_id=team, winner_player_id=player)
