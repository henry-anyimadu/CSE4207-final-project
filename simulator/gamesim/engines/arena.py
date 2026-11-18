"""Arena shooter: free-for-all (or N teams), kills / deaths / assists / damage / headshots.

This is the "state 1" game from the design doc: a small leaderboard that lots
of viewers poll. Each player gets a hidden skill rating so the leaderboard has
a realistic spread instead of everyone tied.
"""

from __future__ import annotations

from ..models import EventType, GameplayEvent, PlayerInfo, PlayerState, TeamInfo, TickPayload, Vec3
from .base import GameEngine

POINTS = {"kill": 100, "assist": 50, "headshot": 25, "death": 0, "damage": 0}
MAP_SIZE = 100.0  # square map, metres
SKIRMISH_PROB = 0.25  # share of actions that are damage-only (no kill)


class ArenaEngine(GameEngine):
    def _setup(self) -> None:
        n = self.spec.players
        names = self.make_names(n)
        nteams = self.spec.teams
        if nteams:
            self.teams = [TeamInfo(team_id=f"t{i + 1}", name=f"Team {i + 1}") for i in range(nteams)]
            self.team_scores = {t.team_id: 0 for t in self.teams}
        self.players = [
            PlayerInfo(
                player_id=f"m{self.match_id}p{i + 1:02d}",
                display_name=names[i],
                team_id=self.teams[i % nteams].team_id if nteams else None,
            )
            for i in range(n)
        ]
        self.ids = [p.player_id for p in self.players]
        self.team_of = {p.player_id: p.team_id for p in self.players}
        self.skill = {pid: self.rng.lognormvariate(0.0, 0.35) for pid in self.ids}
        self.kills = {pid: 0 for pid in self.ids}
        self.points = {pid: 0 for pid in self.ids}
        self.health = {pid: 100 for pid in self.ids}
        self.pos = {pid: self._random_pos() for pid in self.ids}

    # ---- helpers ---------------------------------------------------------------
    def _random_pos(self) -> list[float]:
        return [self.rng.uniform(0, MAP_SIZE), self.rng.uniform(0, MAP_SIZE)]

    def _enemies(self, pid: str) -> list[str]:
        team = self.team_of[pid]
        return [o for o in self.ids if o != pid and (team is None or self.team_of[o] != team)]

    def _pick_attacker(self) -> str:
        return self.rng.choices(self.ids, weights=[self.skill[p] for p in self.ids])[0]

    def _pick_victim(self, attacker: str) -> str:
        enemies = self._enemies(attacker)
        return self.rng.choices(enemies, weights=[1.0 / self.skill[e] for e in enemies])[0]

    def _ev(self, type: EventType, player: str, **kw) -> GameplayEvent:
        pts = kw.pop("points", POINTS.get(type.value, 0))
        self.points[player] += pts
        return self.emit(type, player_id=player, team_id=self.team_of[player], points=pts, **kw)

    def _damage(self, attacker: str, victim: str, amount: int) -> list[GameplayEvent]:
        self.health[victim] = max(0, self.health[victim] - amount)
        if not self.stat_enabled("damage"):
            return []
        return [self._ev(EventType.DAMAGE, attacker, target_player_id=victim, value=amount)]

    # ---- game loop -------------------------------------------------------------
    def on_action(self) -> list[GameplayEvent]:
        attacker = self._pick_attacker()
        victim = self._pick_victim(attacker)
        out: list[GameplayEvent] = []

        if self.rng.random() < SKIRMISH_PROB and self.health[victim] > 30:
            out += self._damage(attacker, victim, self.rng.randint(10, min(60, self.health[victim] - 1)))
            return out

        # helpers: teammates of attacker (team mode) or any third party (FFA)
        helpers = [
            p
            for p in self.ids
            if p not in (attacker, victim)
            and (self.team_of[attacker] is None or self.team_of[p] == self.team_of[attacker])
        ]
        n_assists = self.rng.choices([0, 1, 2], weights=[0.45, 0.40, 0.15])[0]
        assisters = self.rng.sample(helpers, k=min(n_assists, len(helpers)))

        remaining = self.health[victim]
        for a in assisters:
            chunk = self.rng.randint(1, max(1, remaining - 1)) if remaining > 1 else 0
            if chunk:
                out += self._damage(a, victim, chunk)
                remaining -= chunk
        out += self._damage(attacker, victim, max(1, remaining))

        team = self.team_of[attacker]
        out.append(
            self._ev(
                EventType.KILL,
                attacker,
                target_player_id=victim,
                team_score_delta=1 if team else 0,
            )
        )
        if team:
            self.team_scores[team] += 1
        self.kills[attacker] += 1
        out.append(self._ev(EventType.DEATH, victim, target_player_id=attacker))
        if self.stat_enabled("assists"):
            for a in assisters:
                out.append(self._ev(EventType.ASSIST, a, target_player_id=victim))
        if self.stat_enabled("headshots") and self.rng.random() < min(0.6, 0.2 * self.skill[attacker]):
            out.append(self._ev(EventType.HEADSHOT, attacker, target_player_id=victim))

        # victim respawns
        self.health[victim] = 100
        self.pos[victim] = self._random_pos()

        limit = self.spec.score_limit
        if limit:
            reached = (self.team_scores[team] >= limit) if team else (self.kills[attacker] >= limit)
            if reached:
                out += self.finish("score_limit", winner_team_id=team, winner_player_id=attacker)
        return out

    def on_tick(self) -> list[GameplayEvent]:
        states = []
        for pid in self.ids:
            x, y = self.pos[pid]
            x = min(MAP_SIZE, max(0.0, x + self.rng.gauss(0, 1.5)))
            y = min(MAP_SIZE, max(0.0, y + self.rng.gauss(0, 1.5)))
            self.pos[pid] = [x, y]
            if self.health[pid] < 100 and self.rng.random() < 0.1:
                self.health[pid] = min(100, self.health[pid] + 5)  # slow regen
            states.append(
                PlayerState(
                    player_id=pid,
                    pos=Vec3(x=round(x, 2), y=round(y, 2)),
                    health=self.health[pid],
                )
            )
        payload = TickPayload(
            time_remaining_s=self.time_remaining(),
            team_scores=dict(self.team_scores),
            players=states,
        )
        return [self.emit(EventType.TICK, payload=payload)]

    def winners(self) -> tuple[str | None, str | None]:
        best_player = max(self.ids, key=lambda p: (self.points[p], self.kills[p], p))
        best_team = None
        if self.team_scores:
            ranked = sorted(self.team_scores.items(), key=lambda kv: -kv[1])
            if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
                best_team = ranked[0][0]
        return best_team, best_player

    def on_regulation_end(self) -> list[GameplayEvent]:
        team, player = self.winners()
        return self.finish("time_expired", winner_team_id=team, winner_player_id=player)
