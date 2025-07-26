from dataclasses import dataclass, field
from typing import Optional, List, Dict

# --- Game State Components ---

@dataclass
class Player:
    player_id: int
    health: int = 30
    mana_pool: int = 0

@dataclass
class CardInfo:
    name: str
    cost: int
    card_type: str  # "MINION", "SPELL", "LAND"
    attack: Optional[int] = None
    health: Optional[int] = None
    max_health: Optional[int] = None

@dataclass
class SpellEffect:
    effect_type: str
    value: int
    requires_target: bool = False

@dataclass
class Deck:
    card_ids: List[int] = field(default_factory=list)

@dataclass
class Graveyard:
    """Component for a player, holding cards in their graveyard."""
    card_ids: List[int] = field(default_factory=list)

# --- Entity State Components (Markers) ---

@dataclass
class Owner:
    player_entity_id: int

@dataclass
class InHand:
    pass

@dataclass
class OnBoard:
    pass

@dataclass
class InDeck:
    pass

@dataclass
class InGraveyard:
    """Marker component for a card that is in the graveyard."""
    pass

@dataclass
class Tapped:
    pass

@dataclass
class SummoningSickness:
    pass

@dataclass
class Attacking:
    pass

@dataclass
class ActiveTurn:
    pass

@dataclass
class PlayedLandThisTurn:
    pass

@dataclass
class WaitingForBlockers:
    pass

@dataclass
class GameOver:
    winner_player_id: int

# --- Command Components ---

@dataclass
class PlayCardCommand:
    player_entity_id: int
    card_entity_id: int
    target_id: Optional[int] = None

@dataclass
class EndTurnCommand:
    player_entity_id: int

@dataclass
class TapLandCommand:
    player_entity_id: int
    card_entity_id: int

@dataclass
class DeclareBlockersCommand:
    player_entity_id: int
    blocks: Dict[int, int] # {blocker_id: attacker_id}

@dataclass
class DeclareAttackersCommand:
    """Команда, отправляемая для объявления всех атакующих за раз."""
    player_entity_id: int
    attacker_ids: list[int]