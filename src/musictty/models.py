from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    id: str
    title: str  # "Artist — Title" when the artist is known
