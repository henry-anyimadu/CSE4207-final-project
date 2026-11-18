"""Deterministic, made-up gamer tags (seeded RNG -> same names every run)."""

from __future__ import annotations

import random

_ADJ = [
    "Turbo",
    "Silent",
    "Rapid",
    "Crimson",
    "Frosty",
    "Lucky",
    "Neon",
    "Shadow",
    "Cosmic",
    "Iron",
    "Sneaky",
    "Mighty",
    "Pixel",
    "Golden",
    "Rusty",
    "Atomic",
    "Salty",
    "Swift",
    "Wild",
    "Electric",
    "Lunar",
    "Solar",
    "Hyper",
    "Quiet",
    "Brave",
    "Clutch",
    "Laggy",
    "Spicy",
    "Arctic",
    "Velvet",
]
_NOUN = [
    "Falcon",
    "Badger",
    "Wombat",
    "Viper",
    "Otter",
    "Comet",
    "Rocket",
    "Panda",
    "Ghost",
    "Llama",
    "Kraken",
    "Ferret",
    "Taco",
    "Hornet",
    "Walrus",
    "Gecko",
    "Raven",
    "Bison",
    "Moose",
    "Phoenix",
    "Narwhal",
    "Yeti",
    "Mantis",
    "Puffin",
    "Toaster",
    "Goblin",
    "Lynx",
    "Pretzel",
    "Drone",
    "Squid",
]


def gamer_tags(rng: random.Random, n: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < n:
        name = f"{rng.choice(_ADJ)}{rng.choice(_NOUN)}"
        if rng.random() < 0.4:
            name += str(rng.randint(1, 99))
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
