from dataclasses import dataclass, field
from typing import Optional, List, Dict

# --- Game State Components ---

@dataclass
class ManaPool:
    """Хранит ману разных цветов для игрока."""
    W: int = 0  # White
    U: int = 0  # Blue
    B: int = 0  # Black
    R: int = 0  # Red
    G: int = 0  # Green
    C: int = 0  # Colorless/Generic

@dataclass
class Player:
    player_id: int
    health: int = 30
    mana_pool: ManaPool = field(default_factory=ManaPool)

@dataclass
class CardInfo:
    name: str
    card_type: str  # "MINION", "SPELL", "LAND"
    cost: Dict[str, int] = field(default_factory=dict) # e.g. {'G': 1, 'generic': 2}
    produces: Optional[str] = None # e.g. 'W', 'U', 'B', 'R', 'G'
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
class Disconnected:
    """Marker component for a player that is currently disconnected."""
    pass

# --- Mulligan Phase Components & Commands ---
@dataclass
class MulliganCommand:
    """Команда для выполнения муллигана."""
    player_entity_id: int

@dataclass
class KeepHandCommand:
    """Команда для сохранения текущей руки."""
    player_entity_id: int

@dataclass
class PutCardsBottomCommand:
    """Команда для отправки выбранных карт в низ колоды после муллигана."""
    player_entity_id: int
    card_ids: list[int]

@dataclass
class MulliganDecisionPhase:
    """Маркер для игрока, который решает, оставить руку или сделать муллиган."""
    pass

@dataclass
class KeptHand:
    """Маркер для игрока, который решил оставить руку."""
    pass

@dataclass
class MulliganCount:
    """Отслеживает, сколько раз игрок сделал муллиган."""
    count: int = 0

@dataclass
class GamePhaseComponent:
    """Синглтон-компонент для отслеживания общей фазы игры на сервере."""
    phase: str # "MULLIGAN", "GAME_RUNNING"

@dataclass
class Attacking:
    pass

@dataclass
class PlayerReadyCommand:
    """Команда, отправляемая игроком, когда он готов начать игру в лобби."""
    player_entity_id: int

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
    
@dataclass
class ReturnToLobbyCommand:
    player_entity_id: int