import asyncio
import json
import argparse
import sys
import threading
import queue
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum, auto

import pygame
import esper

from .ui import UIManager, Button, Label, CONFIRM_BUTTON_COLOR, CONFIRM_BUTTON_HOVER_COLOR, CONFIRM_BUTTON_PRESSED_COLOR, CONFIRM_BUTTON_TEXT_COLOR, TURN_INDICATOR_PLAYER_COLOR, TURN_INDICATOR_OPPONENT_COLOR

# --- Константы ---
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 720
BG_COLOR = (50, 50, 60)

CARD_WIDTH = 100
CARD_HEIGHT = 140
Y_MARGIN = 7
CARD_SPACING_X = 5 # Расстояние между картами по горизонтали
PORTRAIT_X = 20

# --- Позиции по оси Y ---
# Лог событий (снизу)
LOG_LINE_HEIGHT = 16
LOG_LINES = 3
LOG_HEIGHT = (LOG_LINE_HEIGHT * LOG_LINES) + Y_MARGIN # 70
LOG_Y = SCREEN_HEIGHT - LOG_HEIGHT


# Зона игрока (снизу вверх)
PLAYER_HAND_Y = LOG_Y - CARD_HEIGHT - Y_MARGIN
PLAYER_BOARD_Y = PLAYER_HAND_Y - CARD_HEIGHT - Y_MARGIN

# Зона оппонента (сверху вниз)
OPPONENT_HAND_Y = Y_MARGIN
OPPONENT_BOARD_Y = OPPONENT_HAND_Y + CARD_HEIGHT + Y_MARGIN

CARD_BG_COLOR = (100, 100, 120)
CARD_HIGHLIGHT_COLOR = (255, 255, 0)
CARD_SELECTION_COLOR = (70, 170, 255)  # Светло-голубой для выделения
TARGET_COLOR = (255, 0, 0)

FONT_COLOR = (255, 255, 255)
PLAYER_COLOR = (100, 200, 100)
OPPONENT_COLOR = (200, 100, 100)
ATTACK_READY_COLOR = (0, 255, 0)

# --- Компоненты (Components) ---

class GamePhase(Enum):
    """Определяет текущую фазу хода игрока, следуя логике MTG."""
    MAIN_1 = auto()
    COMBAT_DECLARE_ATTACKERS = auto()
    COMBAT_AWAITING_CONFIRMATION = auto()
    COMBAT_DECLARE_BLOCKERS = auto()
    MAIN_2 = auto()

@dataclass
class ClientState:
    """Singleton component to hold global client state."""
    my_player_id: Optional[int] = None
    active_player_id: Optional[int] = None
    game_state_dict: Dict[str, Any] = None  # Raw state from server
    selected_entity: Optional[int] = None
    hovered_entity: Optional[int] = None
    game_over: bool = False
    winner_id: Optional[int] = None
    player_connection_status: Dict[int, str] = field(default_factory=dict)
    # --- Состояние боя в стиле MTG ---
    phase: GamePhase = GamePhase.MAIN_1
    attackers: List[int] = field(default_factory=list)  # Атакующие, подтвержденные сервером (для защитника)
    pending_attackers: List[int] = field(default_factory=list)  # Атакующие, выбираемые активным игроком
    selected_blocker: Optional[int] = None
    block_assignments: Dict[int, int] = field(default_factory=dict) # {blocker_id: attacker_id}
    # --- Animation State ---
    animation_queue: List[Dict] = field(default_factory=list)
    current_animation: Optional[Dict] = None
    animation_timer: float = 0.0
    # --- Event Log ---
    log_messages: List[str] = field(default_factory=list)
    max_log_messages: int = LOG_LINES

@dataclass
class Position:
    x: float
    y: float

@dataclass
class Drawable:
    sprite: pygame.sprite.Sprite

@dataclass
class Clickable:
    """Marker component for entities that can be clicked."""
    pass

# --- Спрайт карты (Card Sprite) ---
# This class is mostly the same, but now it's just a visual representation.
class CardSprite(pygame.sprite.Sprite):
    """Визуальное представление карты в игре."""
    def __init__(self, card_id: int, card_data: Dict[str, Any], font: pygame.font.Font):
        super().__init__()
        self.card_id = card_id
        self.card_data = card_data
        self.font = font
        self.image = pygame.Surface([CARD_WIDTH, CARD_HEIGHT])
        self.rect = self.image.get_rect()
        self.update_visuals() # Initial draw with no highlights

    def update_visuals(self, is_hovered: bool = False, is_selected: bool = False):
        """Перерисовывает внешний вид карты на основе ее данных."""
        if self.card_data.get('is_hidden', False):
            # --- Рисуем рубашку карты ---
            self.image.fill((139, 69, 19)) # SaddleBrown
            pygame.draw.rect(self.image, (0, 0, 0), self.image.get_rect(), 5)
            pygame.draw.circle(self.image, (218, 165, 32), self.image.get_rect().center, 30) # Goldenrod
        else:
            # --- Рисуем лицевую сторону карты ---
            self.image.fill(CARD_BG_COLOR)
            # Добавляем базовую рамку для визуального разделения карт при наложении
            pygame.draw.rect(self.image, (0, 0, 0), self.image.get_rect(), 2)

            name = self.card_data.get('name', '???')
            cost = self.card_data.get('cost', '?')
            card_type = self.card_data.get('type')

            name_text = self.font.render(name, True, FONT_COLOR)
            cost_text = self.font.render(f"({cost})", True, FONT_COLOR)
            self.image.blit(name_text, (5, 5))
            self.image.blit(cost_text, (CARD_WIDTH - cost_text.get_width() - 5, 5))

            if card_type == 'MINION':
                attack = self.card_data.get('attack', '?')
                health = self.card_data.get('health', '?')
                stats_text = self.font.render(f"{attack}/{health}", True, FONT_COLOR)
                self.image.blit(stats_text, (CARD_WIDTH - stats_text.get_width() - 5, CARD_HEIGHT - 25))
            elif card_type == 'SPELL':
                type_text = self.font.render("Spell", True, FONT_COLOR)
                self.image.blit(type_text, (5, CARD_HEIGHT - 25))

            if self.card_data.get('has_sickness'):
                sickness_overlay = pygame.Surface(self.image.get_size(), pygame.SRCALPHA)
                sickness_overlay.fill((128, 128, 128, 100))  # Полупрозрачный серый
                self.image.blit(sickness_overlay, (0, 0))
                zzz_text = self.font.render("Zzz", True, (200, 200, 255))
                self.image.blit(zzz_text, (5, CARD_HEIGHT - 45))

            if self.card_data.get('is_tapped'):
                tapped_overlay = pygame.Surface(self.image.get_size(), pygame.SRCALPHA)
                tapped_overlay.fill((0, 0, 0, 100))  # Полупрозрачный черный
                self.image.blit(tapped_overlay, (0, 0))

            if self.card_data.get('can_attack', False):
                pygame.draw.circle(self.image, ATTACK_READY_COLOR, (15, CARD_HEIGHT - 15), 10)

        # --- Отрисовка рамок поверх всего ---
        # Красная рамка для подтвержденных атакующих (высший приоритет)
        if self.card_data.get('is_attacking'):
            pygame.draw.rect(self.image, (255, 60, 60), self.image.get_rect(), 4)

        # Синяя рамка для выбранных карт (атака, блок, цель заклинания)
        if is_selected:
            pygame.draw.rect(self.image, CARD_SELECTION_COLOR, self.image.get_rect(), 4)

        # Желтая рамка для наведения (рисуется поверх синей, если оба состояния активны)
        if is_hovered:
            pygame.draw.rect(self.image, CARD_HIGHLIGHT_COLOR, self.image.get_rect(), 3)

# --- Системы (Systems) ---

class StateUpdateSystem(esper.Processor):
    """Processes messages from the server and updates the client's ECS world."""
    def __init__(self, incoming_q: queue.Queue, font: pygame.font.Font, client_state: ClientState):
        self.incoming_q = incoming_q
        self.font = font
        self.client_state = client_state

    def _add_log_message(self, message: str):
        """Добавляет сообщение в лог и обрезает его до максимального размера."""
        if not message: return
        log = self.client_state.log_messages
        log.append(message)
        if len(log) > self.client_state.max_log_messages:
            self.client_state.log_messages = log[-self.client_state.max_log_messages:]

    def _get_entity_name(self, entity_id: int) -> str:
        """Возвращает имя сущности (игрока или карты) по ID."""
        if not self.client_state.game_state_dict or entity_id is None:
            return f"Сущность {entity_id}"

        # Проверяем, игрок ли это
        for p_id, p_data in self.client_state.game_state_dict.get("players", {}).items():
            if p_data.get("entity_id") == entity_id:
                my_player_id = self.client_state.my_player_id
                if int(p_id) == my_player_id:
                    return "Вы"
                else:
                    return f"Оппонент"

        # Проверяем, карта ли это
        card_data = self.client_state.game_state_dict.get("cards", {}).get(str(entity_id))
        if card_data and not card_data.get("is_hidden"):
            return card_data.get("name", f"Карта {entity_id}")
        return f"Неизвестная цель"
 
    def process(self, *args, **kwargs):
        try:
            while True:
                event = self.incoming_q.get_nowait()
                event_type = event.get("type")

                if event_type == "ASSIGN_PLAYER_ID":
                    self.client_state.my_player_id = event["payload"]["player_id"]
                
                elif event_type == "FULL_STATE_UPDATE":
                    game_state_dict = event.get("payload", {})
                    self.client_state.game_state_dict = game_state_dict
                    self.client_state.active_player_id = game_state_dict.get("active_player_id")
                    # Полное обновление состояния синхронизирует мир, но не должно менять текущую фазу хода.
                    # Фаза меняется только специальными событиями (TURN_STARTED, COMBAT_RESOLVED и т.д.),
                    # поэтому мы не сбрасываем self.client_state.phase здесь.
                    self.client_state.pending_attackers.clear()
                    self.client_state.game_over = False
                    self.client_state.winner_id = None

                    # Сбрасываем выбор, так как все сущности будут пересозданы
                    self.client_state.selected_entity = None
                    self.client_state.selected_blocker = None

                    # Инициализируем статусы подключения.
                    # Все игроки в состоянии считаются подключенными, если их статус еще не известен.
                    for player_id_str in game_state_dict.get("players", {}).keys():
                        player_id = int(player_id_str)
                        if player_id not in self.client_state.player_connection_status:
                            self.client_state.player_connection_status[player_id] = "CONNECTED"

                    self._synchronize_world(game_state_dict)

                elif event_type == "GAME_OVER":
                    payload = event.get("payload", {})
                    self.client_state.game_over = True
                    self.client_state.winner_id = payload.get("winner_id")

                elif event_type in ["CONNECTION_FAILED", "DISCONNECTED"]:
                    print(f"Network status: {event_type}")
                    # A more robust implementation would set a flag to exit the game loop
                    # For now, we assume the main loop will handle this.
                elif event_type == "PLAYER_DISCONNECTED":
                    player_id = event['payload']['player_id']
                    self.client_state.player_connection_status[player_id] = "DISCONNECTED"
                    print(f"--- Игрок {player_id} отключился. Ожидание переподключения... ---")
                elif event_type == "PLAYER_RECONNECTED":
                    player_id = event['payload']['player_id']
                    self.client_state.player_connection_status[player_id] = "CONNECTED"
                    print(f"--- Игрок {player_id} переподключился! ---")
                elif event_type == "PLAYER_MANA_POOL_UPDATED":
                    payload = event.get("payload", {})
                    player_id = payload.get("player_id")
                    new_pool = payload.get("new_mana_pool")
                    if player_id is not None and self.client_state.game_state_dict:
                        player_data = self.client_state.game_state_dict.get("players", {}).get(str(player_id))
                        if player_data:
                            player_data["mana_pool"] = new_pool
                
                elif event_type == "ACTION_ERROR":
                    message = event.get("payload", {}).get("message", "Произошла ошибка")
                    self._add_log_message(f"Ошибка: {message}")

                elif event_type == "PLAYER_DAMAGED":
                    self.client_state.animation_queue.append(event)
                    payload = event.get("payload", {})
                    player_id = payload.get("player_id")
                    new_health = payload.get("new_health")
                    if player_id is not None and new_health is not None and self.client_state.game_state_dict:
                        player_data = self.client_state.game_state_dict.get("players", {}).get(str(player_id))
                        if player_data:
                            player_data["health"] = new_health
                    
                    source_id = payload.get("source_card_id") or payload.get("attacker_id")
                    source_name = self._get_entity_name(source_id)
                    target_name = self._get_entity_name(player_id)
                    self._add_log_message(f"{target_name} получает урон от {source_name}.")

                elif event_type == "CARD_ATTACKED":
                    self.client_state.animation_queue.append(event)
                    payload = event.get("payload", {})
                    all_cards = self.client_state.game_state_dict.get("cards", {})
                    attacker_id, attacker_hp = payload.get("attacker_id"), payload.get("attacker_new_health")
                    target_id, target_hp = payload.get("target_id"), payload.get("target_new_health")
                    if attacker_id and attacker_hp is not None and str(attacker_id) in all_cards:
                        all_cards[str(attacker_id)]["health"] = attacker_hp
                    if target_id and target_hp is not None and str(target_id) in all_cards:
                        all_cards[str(target_id)]["health"] = target_hp
                    
                    attacker_name = self._get_entity_name(attacker_id)
                    target_name = self._get_entity_name(target_id)
                    self._add_log_message(f"{attacker_name} сражается с {target_name}.")

                elif event_type == "BLOCKERS_PHASE_STARTED":
                    payload = event.get("payload", {})
                    attacker_ids = payload.get("attackers", [])
                    self.client_state.phase = GamePhase.COMBAT_DECLARE_BLOCKERS
                    self.client_state.attackers = attacker_ids
                    self.client_state.pending_attackers.clear()  # Атака объявлена, очищаем список кандидатов

                    # Обновляем локальное состояние карт, чтобы они считались атакующими.
                    # Это необходимо для корректной отрисовки (например, красной рамки).
                    if self.client_state.game_state_dict:
                        all_cards = self.client_state.game_state_dict.get("cards", {})
                        for card_id in attacker_ids:
                            card_data = all_cards.get(str(card_id))
                            if card_data:
                                card_data["is_attacking"] = True
                                # Также помечаем их как повернутых для корректной отрисовки
                                card_data["is_tapped"] = True

                    # Reset any previous blocking state
                    self.client_state.selected_blocker = None
                    self.client_state.block_assignments.clear()
                    print(f"--- Началась фаза блокирования. Атакующие: {self.client_state.attackers} ---")

                elif event_type == "COMBAT_RESOLVED":
                    # Сбрасываем флаг атаки у существ, которые участвовали в бою.
                    if self.client_state.game_state_dict:
                        all_cards = self.client_state.game_state_dict.get("cards", {})
                        for card_id in self.client_state.attackers:
                            card_data = all_cards.get(str(card_id))
                            if card_data:
                                card_data["is_attacking"] = False
                    # После боя наступает вторая главная фаза
                    self.client_state.phase = GamePhase.MAIN_2
                    self.client_state.attackers.clear()
                    self.client_state.selected_blocker = None
                    self.client_state.block_assignments.clear()
                    print("--- Бой завершен ---")

                elif event_type == "TURN_ENDED":
                    # При завершении хода сбрасываем состояние до начального для следующего игрока
                    # (хотя TURN_STARTED сделает то же самое, это для надежности)
                    self.client_state.phase = GamePhase.MAIN_1
                    self.client_state.attackers.clear()
                    self.client_state.pending_attackers.clear()
                    self.client_state.selected_blocker = None
                    self.client_state.block_assignments.clear()

                elif event_type == "TURN_STARTED":
                    # A new turn has begun for someone. Update the active player.
                    payload = event.get("payload", {})
                    self.client_state.active_player_id = payload.get("player_id")
                    self.client_state.phase = GamePhase.MAIN_1
                    self.client_state.pending_attackers.clear()
                    player_name = self._get_entity_name(payload.get("player_id"))
                    self._add_log_message(f"Начался ход игрока {self.client_state.active_player_id}.")

                elif event_type == "CARD_MOVED":
                    payload = event.get("payload", {})
                    if payload.get("from") == "HAND" and payload.get("to") == "BOARD":
                        card_id = payload.get("card_id")
                        card_name = self._get_entity_name(card_id)
                        player_name = self._get_entity_name(self.client_state.active_player_id)
                        self._add_log_message(f"{player_name} разыгрывает '{card_name}'.")

                elif event_type == "CARD_DIED":
                    card_id = event['payload'].get('card_id')
                    self.client_state.animation_queue.append(event)
                    card_name = self._get_entity_name(card_id)
                    self._add_log_message(f"'{card_name}' уничтожена.")
                    # Deletion is now handled by AnimationSystem after the animation plays.
        except queue.Empty:
            pass

    def _synchronize_world(self, state: Dict[str, Any]):
        """Re-creates the client world based on server state."""
        # Полностью очищаем мир esper. Это сбрасывает счетчик ID сущностей.
        esper.clear_database()

        # Собираем все ID сущностей с сервера (игроки и карты)
        all_cards_data = state.get("cards", {})
        all_players_data = state.get("players", {})

        all_server_entity_ids = set()
        for player_data in all_players_data.values():
            if "entity_id" in player_data:
                all_server_entity_ids.add(player_data["entity_id"])
        for card_id_str in all_cards_data.keys():
            all_server_entity_ids.add(int(card_id_str))
        
        if not all_server_entity_ids:
            return # Нечего синхронизировать

        # Ключевое исправление: создаем все сущности до максимального ID,
        # чтобы "заполнить пробелы" в ID, возникшие после удаления карт на сервере.
        max_id = max(all_server_entity_ids)
        for i in range(1, max_id + 1):
            new_id = esper.create_entity()
            if new_id != i:
                # Эта ошибка не должна происходить после clear_database(), но проверка полезна.
                print(f"КРИТИЧЕСКАЯ ОШИБКА СИНХРОНИЗАЦИИ: Ожидался ID {i}, но создан {new_id}.")
                return # Прерываем синхронизацию, чтобы избежать дальнейших ошибок

        # Теперь, когда все сущности существуют, добавляем им компоненты (только для карт).
        # ВАЖНО: Мы создаем видимые сущности только для тех карт, которые должны
        # отображаться: карты на столе (любого игрока) и карты в нашей руке.
        # Карты в колодах или в руке противника не должны иметь Drawable компонента.
        for card_id_str, card_data in all_cards_data.items():
            card_location = card_data.get("location")
            card_owner_id = card_data.get("owner_id")

            is_on_board = (card_location == "BOARD")
            is_in_my_hand = (card_location == "HAND" and card_owner_id == self.client_state.my_player_id)
            is_in_opp_hand = (card_location == "HAND" and card_owner_id != self.client_state.my_player_id)

            if is_on_board or is_in_my_hand or is_in_opp_hand:
                card_id = int(card_id_str)
                card_sprite = CardSprite(card_id, card_data, self.font)
                esper.add_component(card_id, Drawable(card_sprite))
                esper.add_component(card_id, Position(0, 0))  # Будет установлено LayoutSystem
                # Карты противника в руке некликабельны
                if not is_in_opp_hand:
                    esper.add_component(card_id, Clickable())

class LayoutSystem(esper.Processor):
    """Calculates and sets the Position component for drawable entities."""
    def __init__(self, client_state: ClientState):
        self.client_state = client_state

    def process(self, *args, **kwargs):
        client_state = self.client_state

        if not client_state.game_state_dict: return

        # Определяем константы для ограничения ширины зон
        BOARD_WIDTH_LIMIT = SCREEN_WIDTH - 2 * (PORTRAIT_X + CARD_WIDTH) - 40  # Пространство между портретами
        HAND_WIDTH_LIMIT = SCREEN_WIDTH - 40 # Почти вся ширина экрана
        
        my_id = client_state.my_player_id
        if my_id is None: return

        all_players = client_state.game_state_dict.get("players", {})
        my_player_data = all_players.get(str(my_id))
        opp_id_str = next((pid for pid in all_players if int(pid) != my_id), None)
        opp_player_data = all_players.get(opp_id_str) if opp_id_str else None

        def arrange_cards(card_ids: List[int], y_pos: int, width_limit: int):
            # Фильтруем карты, которые могут быть удалены из мира событием (например, CARD_DIED)
            # до того, как система расположения успеет отработать.
            drawable_card_ids = [
                cid for cid in card_ids if esper.entity_exists(cid) and esper.has_component(cid, Drawable)
            ]
            num_cards = len(drawable_card_ids)
            if num_cards == 0:
                return

            # Рассчитываем требуемую ширину без наложения
            full_width = num_cards * CARD_WIDTH + (num_cards - 1) * CARD_SPACING_X
            
            spacing = CARD_WIDTH + CARD_SPACING_X
            
            # Если карты не помещаются, рассчитываем новое расстояние с наложением
            if full_width > width_limit and num_cards > 1:
                spacing = (width_limit - CARD_WIDTH) / (num_cards - 1)

            total_width = (num_cards - 1) * spacing + CARD_WIDTH
            start_x = (SCREEN_WIDTH - total_width) / 2
            for i, card_id in enumerate(drawable_card_ids):
                pos = esper.component_for_entity(card_id, Position)
                pos.x = start_x + i * spacing
                pos.y = y_pos

        if my_player_data:
            arrange_cards(my_player_data.get("hand", []), PLAYER_HAND_Y, HAND_WIDTH_LIMIT)
            arrange_cards(my_player_data.get("board", []), PLAYER_BOARD_Y, BOARD_WIDTH_LIMIT)
        
        if opp_player_data:
            arrange_cards(opp_player_data.get("board", []), OPPONENT_BOARD_Y, BOARD_WIDTH_LIMIT)
            arrange_cards(opp_player_data.get("hand", []), OPPONENT_HAND_Y, HAND_WIDTH_LIMIT)

class AnimationSystem(esper.Processor):
    """Processes and times animations for combat and other events."""
    def __init__(self, client_state: ClientState):
        self.client_state = client_state
        self.animation_duration = 0.6  # seconds per animation step

    def process(self, *args, **kwargs):
        client_state = self.client_state
        delta_time = kwargs.get("delta_time", 1/60.0)

        # If there's an ongoing animation, let it finish
        if client_state.current_animation is not None:
            client_state.animation_timer -= delta_time
            if client_state.animation_timer <= 0:
                # Animation finished. Check if it was a death animation that needs cleanup.
                if client_state.current_animation.get("type") == "CARD_DIED":
                    card_id = client_state.current_animation['payload'].get('card_id')
                    if card_id and esper.entity_exists(card_id):
                        # The card is moved to the graveyard on the server.
                        # On the client, we just make it invisible until the next state sync.
                        if esper.has_component(card_id, Drawable):
                            esper.remove_component(card_id, Drawable)

                client_state.current_animation = None
            return

        # If no animation is running, start the next one from the queue
        if client_state.animation_queue:
            client_state.current_animation = client_state.animation_queue.pop(0)
            client_state.animation_timer = self.animation_duration

class UISetupSystem(esper.Processor):
    """Prepares UI elements for the current frame before input is handled."""
    def __init__(self, client_state: ClientState, ui_manager: UIManager, font: pygame.font.Font, medium_font: pygame.font.Font):
        self.client_state = client_state
        self.ui_manager = ui_manager
        self.font = font
        self.medium_font = medium_font

    def process(self, *args, **kwargs):
        # Clear UI from the previous frame
        self.ui_manager.clear_elements()

        # Setup UI for the current frame if the game is running
        if not self.client_state.game_over and self.client_state.game_state_dict:
            self._setup_ui(self.client_state)

    def _setup_ui(self, client_state: ClientState):
        """Creates and adds all necessary UI elements to the UIManager for the current frame based on game state."""
        if client_state.active_player_id is None:
            return

        is_my_turn = client_state.active_player_id == client_state.my_player_id
        vertical_center_y = (OPPONENT_BOARD_Y + CARD_HEIGHT + PLAYER_BOARD_Y) // 2
        
        phase_text = ""
        
        # 1. Определяем текст индикатора фазы
        if not is_my_turn:
            phase_text = "ХОД ОППОНЕНТА"
            if client_state.phase == GamePhase.COMBAT_DECLARE_BLOCKERS:
                phase_text = "ОБЪЯВИТЕ БЛОКЕРОВ"
        else:  # Мой ход
            if client_state.phase == GamePhase.MAIN_1:
                phase_text = "ГЛАВНАЯ ФАЗА 1"
            elif client_state.phase == GamePhase.COMBAT_DECLARE_ATTACKERS:
                phase_text = "ФАЗА АТАКИ"
            elif client_state.phase == GamePhase.COMBAT_DECLARE_BLOCKERS:
                phase_text = "Оппонент блокирует..."
            elif client_state.phase == GamePhase.MAIN_2:
                phase_text = "ГЛАВНАЯ ФАЗА 2"

        # 2. Отображаем индикатор фазы
        if phase_text:
            turn_color = TURN_INDICATOR_PLAYER_COLOR if is_my_turn else TURN_INDICATOR_OPPONENT_COLOR
            turn_label = Label(phase_text, (PORTRAIT_X, vertical_center_y - self.font.get_height() // 2), self.font, turn_color, center=False)
            self.ui_manager.add_element(turn_label)

        # 3. Отображаем кнопки действий в зависимости от фазы
        input_system = esper.get_processor(InputSystem)
        button_rect = pygame.Rect(SCREEN_WIDTH // 2 - 125, vertical_center_y - 25, 250, 50)

        # Кнопка для защищающегося игрока
        if not is_my_turn and client_state.phase == GamePhase.COMBAT_DECLARE_BLOCKERS:
            def confirm_blocks_callback():
                input_system.outgoing_q.put({
                    "type": "DECLARE_BLOCKERS",
                    "payload": {"blocks": client_state.block_assignments}
                })
            confirm_button = Button(
                "Подтвердить блоки", button_rect, self.font, confirm_blocks_callback,
                bg_color=CONFIRM_BUTTON_COLOR,
                hover_color=CONFIRM_BUTTON_HOVER_COLOR,
                pressed_color=CONFIRM_BUTTON_PRESSED_COLOR,
                text_color=CONFIRM_BUTTON_TEXT_COLOR
            )
            self.ui_manager.add_element(confirm_button)
        
        # Кнопки для активного игрока
        if is_my_turn:
            if client_state.phase == GamePhase.MAIN_1:
                def go_to_combat_callback(): client_state.phase = GamePhase.COMBAT_DECLARE_ATTACKERS
                button = Button("Перейти к атаке", button_rect, self.font, go_to_combat_callback)
                self.ui_manager.add_element(button)
            elif client_state.phase == GamePhase.COMBAT_DECLARE_ATTACKERS:
                def declare_attackers_callback(): input_system.declare_attackers()
                button = Button("Подтвердить атакующих", button_rect, self.font, declare_attackers_callback)
                self.ui_manager.add_element(button)
            elif client_state.phase == GamePhase.COMBAT_AWAITING_CONFIRMATION:
                # Пока ждем ответа сервера, показываем текст и не даем нажимать кнопки.
                button = Label("Ожидание ответа сервера...", (SCREEN_WIDTH // 2, vertical_center_y), self.font, (200, 200, 200), center=True)
                self.ui_manager.add_element(button)
            elif client_state.phase == GamePhase.MAIN_2:
                def end_turn_callback(): input_system.outgoing_q.put({"type": "END_TURN"})
                button = Button("Завершить ход", button_rect, self.font, end_turn_callback)
                self.ui_manager.add_element(button)

class InputSystem(esper.Processor):
    """Handles user input and creates commands to be sent to the server."""
    def __init__(self, outgoing_q: queue.Queue, client_state: ClientState, ui_manager: UIManager):
        self.outgoing_q = outgoing_q
        self.client_state = client_state
        self.ui_manager = ui_manager

    def declare_attackers(self):
        """Отправляет на сервер список выбранных атакующих и переводит клиента в состояние ожидания."""
        client_state = self.client_state

        # Если атакующих не выбрано, не нужно отправлять запрос на сервер.
        # Сразу переходим ко второй главной фазе. Это решает проблему "зависания",
        # если сервер не обрабатывает пустой список атакующих.
        if not client_state.pending_attackers:
            client_state.phase = GamePhase.MAIN_2
            return

        self.outgoing_q.put({
            "type": "DECLARE_ATTACKERS",
            "payload": {"attacker_ids": client_state.pending_attackers}
        })
        # Переводим клиента в состояние ожидания ответа от сервера.
        # Это предотвращает повторные нажатия и решает проблему рассинхронизации.
        # Сервер должен прислать `BLOCKERS_PHASE_STARTED`, чтобы сменить фазу.
        client_state.phase = GamePhase.COMBAT_AWAITING_CONFIRMATION

    def process(self, *args, **kwargs):
        client_state = self.client_state

        # Обновляем информацию о наведении мыши каждый кадр, а не только по событию
        self._handle_mouse_motion(pygame.mouse.get_pos(), client_state)

        for event in pygame.event.get():
            # Let the UI Manager process the event first. If it handles it, we skip the game logic for this event.
            if self.ui_manager.process_event(event):
                continue

            if event.type == pygame.QUIT:
                self.outgoing_q.put(None) # Signal network thread to close
                client_state.my_player_id = -999 # Сигнал для выхода из главного цикла
                return

            # Если игра окончена, игнорируем все остальные события ввода
            if client_state.game_over:
                continue
            
            if event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1: # Left click
                    self._handle_left_click(event.pos, client_state)
                elif event.button == 3: # Right click
                    self._handle_right_click(client_state) # This now also cancels selections

    def _handle_mouse_motion(self, pos, client_state: ClientState):
        """Обрабатывает движение мыши для определения, на какую карту наведен курсор."""
        # Находим все карты под курсором
        colliding_cards = []
        # get_components не гарантирует порядок, поэтому мы должны сами найти верхнюю карту
        for ent, (drawable, position) in esper.get_components(Drawable, Position):
            if drawable.sprite.rect.collidepoint(pos):
                colliding_cards.append(ent)

        if not colliding_cards:
            client_state.hovered_entity = None
            return

        # Если под курсором несколько карт (из-за наложения), выбираем верхнюю.
        # В нашей игре верхние карты имеют больший Y (ближе к игроку).
        # Это простое правило, которое работает для руки и стола игрока.
        top_card = -1
        max_y = -1
        for card_id in colliding_cards:
            card_pos = esper.component_for_entity(card_id, Position)
            if card_pos.y > max_y:
                max_y = card_pos.y
                top_card = card_id
        
        client_state.hovered_entity = top_card

    def _handle_left_click(self, pos, client_state: ClientState):
        # --- Phase-specific logic first ---
        is_my_turn = client_state.active_player_id == client_state.my_player_id
        if not is_my_turn:
            if client_state.phase == GamePhase.COMBAT_DECLARE_BLOCKERS:
                self._handle_blocking_click(pos, client_state)
            return  # Не мой ход, больше ничего не делаем

        # --- Find what was clicked (card or portrait) ---
        clicked_card_entity = None
        for ent, (drawable, position) in esper.get_components(Drawable, Position):
            if drawable.sprite.rect.collidepoint(pos):
                clicked_card_entity = ent
                break

        clicked_player_entity = None
        # Only check for portrait if no card was clicked, to avoid overlap issues
        if not clicked_card_entity:
            opp_id_str = next((pid for pid in client_state.game_state_dict["players"] if int(pid) != client_state.my_player_id), None)
            if opp_id_str:
                render_system = esper.get_processor(RenderSystem)
                opp_portrait_rect = render_system._get_player_portrait_rect(int(opp_id_str))
                if opp_portrait_rect and opp_portrait_rect.collidepoint(pos):
                    clicked_player_entity = client_state.game_state_dict["players"][opp_id_str].get("entity_id")

        # --- Handle spell targeting (if a spell is selected) ---
        # Розыгрыш заклинаний с таргетом возможен только в главные фазы
        if client_state.phase in [GamePhase.MAIN_1, GamePhase.MAIN_2]:
            if client_state.selected_entity is not None:
                target_id = clicked_card_entity or clicked_player_entity
                if target_id:
                    # Assuming selected_entity is a spell card
                    self.outgoing_q.put({"type": "PLAY_CARD", "payload": {"card_entity_id": client_state.selected_entity, "target_id": target_id}})

                # Deselect after any action (or misclick)
                client_state.selected_entity = None
                return

        # --- Handle first click on a card (nothing is selected) ---
        if clicked_card_entity:
            self._handle_card_click(clicked_card_entity, client_state)

    def _handle_card_click(self, clicked_entity, client_state: ClientState):
        is_my_turn = client_state.active_player_id == client_state.my_player_id
        drawable = esper.component_for_entity(clicked_entity, Drawable)
        card_data = drawable.sprite.card_data
        is_my_card = card_data.get("owner_id") == client_state.my_player_id

        if not is_my_turn or not is_my_card:
            return

        location = card_data.get("location")
        card_type = card_data.get("type")

        # --- Логика для Главных Фаз (розыгрыш карт) ---
        if client_state.phase in [GamePhase.MAIN_1, GamePhase.MAIN_2]:
            if location == "HAND":
                if card_type == "SPELL":
                    full_card_data = client_state.game_state_dict.get("cards", {}).get(str(clicked_entity), {})
                    spell_effect = full_card_data.get("effect", {})
                    if spell_effect.get("requires_target"):
                        client_state.selected_entity = clicked_entity  # Выбираем для таргетинга
                    else:  # Разыгрываем заклинание без цели
                        self.outgoing_q.put({"type": "PLAY_CARD", "payload": {"card_entity_id": clicked_entity}})
                else:  # Разыгрываем существо или землю
                    self.outgoing_q.put({"type": "PLAY_CARD", "payload": {"card_entity_id": clicked_entity}})
            elif location == "BOARD" and card_type == "LAND" and not card_data.get("is_tapped"):
                self.outgoing_q.put({"type": "TAP_LAND", "payload": {"card_entity_id": clicked_entity}})
        
        # --- Логика для Фазы Объявления Атакующих ---
        elif client_state.phase == GamePhase.COMBAT_DECLARE_ATTACKERS:
            if location == "BOARD" and card_type == "MINION" and card_data.get("can_attack"):
                if clicked_entity in client_state.pending_attackers:
                    client_state.pending_attackers.remove(clicked_entity)  # Отменить выбор
                else:
                    client_state.pending_attackers.append(clicked_entity)  # Выбрать для атаки
 
    def _handle_blocking_click(self, pos, client_state: ClientState):
        """Handles clicks during the blocking phase."""
        # Double-check: only the non-active player (defender) can assign blockers.
        is_my_turn = client_state.active_player_id == client_state.my_player_id
        if is_my_turn:
            return # Attackers cannot perform block actions.

        clicked_entity = None
        # Find what was clicked
        for ent, (drawable, position) in esper.get_components(Drawable, Position):
            if drawable.sprite.rect.collidepoint(pos):
                clicked_entity = ent
                break
        
        if not clicked_entity:
            return # Clicked on empty space

        clicked_card_data = esper.component_for_entity(clicked_entity, Drawable).sprite.card_data

        if client_state.selected_blocker is None:
            # --- Attempting to select a blocker ---
            is_my_card = clicked_card_data.get("owner_id") == client_state.my_player_id
            is_on_board = clicked_card_data.get("location") == "BOARD"
            is_creature = clicked_card_data.get("type") == "MINION"
            is_tapped = clicked_card_data.get("is_tapped")
            
            if is_my_card and is_on_board and is_creature and not is_tapped:
                # If we click a blocker that is already assigned, unassign it.
                if clicked_entity in client_state.block_assignments:
                    del client_state.block_assignments[clicked_entity]
                else:
                    client_state.selected_blocker = clicked_entity
        else:
            # --- A blocker is already selected, attempting to assign it to an attacker ---
            is_attacker = clicked_entity in client_state.attackers
            
            if is_attacker:
                client_state.block_assignments[client_state.selected_blocker] = clicked_entity
                client_state.selected_blocker = None # Deselect after assigning
            elif clicked_entity == client_state.selected_blocker:
                # Clicking the selected blocker again deselects it
                client_state.selected_blocker = None
            else:
                # Clicked something else, maybe another potential blocker? Let's just deselect for simplicity.
                client_state.selected_blocker = None

    def _handle_right_click(self, client_state):
        # Right click now only cancels spell selection
        client_state.selected_entity = None


class RenderSystem(esper.Processor):
    """Draws all entities and UI elements."""
    def __init__(self, screen: pygame.Surface, client_state: ClientState, ui_manager: UIManager,
                 font: pygame.font.Font, medium_font: pygame.font.Font, log_font: pygame.font.Font,
                 big_font: pygame.font.Font, emoji_font: pygame.font.Font):
        self.screen = screen
        self.client_state = client_state
        self.ui_manager = ui_manager
        self.font = font
        self.medium_font = medium_font
        self.log_font = log_font
        self.big_font = big_font
        self.emoji_font = emoji_font

    def process(self, *args, **kwargs):
        client_state = self.client_state
        self.screen.fill(BG_COLOR)

        if client_state.game_over:
            self._draw_game_over_screen(client_state)
        else:
            hovered_card_to_draw = None
            hovered_entity_id = client_state.hovered_entity

            # --- Собираем все рисуемые карты ---
            all_drawable_cards = []
            for ent, (drawable, pos) in esper.get_components(Drawable, Position):
                all_drawable_cards.append((ent, drawable, pos))

            # --- Сортируем карты по X-координате (слева направо) ---
            all_drawable_cards.sort(key=lambda item: item[2].x)

            # --- Рисуем карты в обратном порядке (справа налево) ---
            # Это гарантирует, что левые карты будут нарисованы поверх правых,
            # и правая часть карт (со статистикой) останется видимой.
            # Вы были правы: важен именно порядок вывода, а не сортировки.
            for ent, drawable, pos in reversed(all_drawable_cards):
                # Если это карта под курсором, откладываем ее отрисовку
                if ent == hovered_entity_id:
                    hovered_card_to_draw = (drawable, pos)
                    continue

                is_selected = (ent == client_state.selected_entity or
                               ent == client_state.selected_blocker or
                               (client_state.phase == GamePhase.COMBAT_DECLARE_ATTACKERS and ent in client_state.pending_attackers))

                drawable.sprite.update_visuals(is_hovered=False, is_selected=is_selected)
                drawable.sprite.rect.center = (pos.x + CARD_WIDTH / 2, pos.y + CARD_HEIGHT / 2)
                self.screen.blit(drawable.sprite.image, drawable.sprite.rect)

            # Рисуем карту под курсором последней, чтобы она была поверх всех, и со смещением
            if hovered_card_to_draw:
                drawable, pos = hovered_card_to_draw
                # Проверяем, выбрана ли карта под курсором, чтобы передать оба состояния
                is_selected = (hovered_entity_id == client_state.selected_entity or
                               hovered_entity_id == client_state.selected_blocker or
                               (client_state.phase == GamePhase.COMBAT_DECLARE_ATTACKERS and hovered_entity_id in client_state.pending_attackers))
                drawable.sprite.update_visuals(is_hovered=True, is_selected=is_selected)
                # Создаем временный Rect для отрисовки со смещением, чтобы не менять исходный Rect спрайта
                # и избежать "прыгания" карты.
                temp_rect = drawable.sprite.rect.copy()
                temp_rect.center = (pos.x + CARD_WIDTH / 2, pos.y + CARD_HEIGHT / 2 - 20) # Приподнимаем карту
                self.screen.blit(drawable.sprite.image, temp_rect)

            # Draw targeting line for ATTACKING
            if client_state.selected_entity is not None and esper.has_component(client_state.selected_entity, Position):
                selected_pos = esper.component_for_entity(client_state.selected_entity, Position)
                start_pos = (selected_pos.x + CARD_WIDTH / 2, selected_pos.y + CARD_HEIGHT / 2)
                end_pos = pygame.mouse.get_pos()
                pygame.draw.line(self.screen, TARGET_COLOR, start_pos, end_pos, 3)

            # Draw targeting line for BLOCKING
            if client_state.selected_blocker is not None and esper.has_component(client_state.selected_blocker, Position):
                selected_pos = esper.component_for_entity(client_state.selected_blocker, Position)
                start_pos = (selected_pos.x + CARD_WIDTH / 2, selected_pos.y + CARD_HEIGHT / 2)
                end_pos = pygame.mouse.get_pos()
                pygame.draw.line(self.screen, (0, 0, 255), start_pos, end_pos, 3) # Blue line for blocking

            # Draw assigned block lines
            for blocker_id, attacker_id in client_state.block_assignments.items():
                if esper.has_component(blocker_id, Position) and esper.has_component(attacker_id, Position):
                    blocker_pos = esper.component_for_entity(blocker_id, Position)
                    attacker_pos = esper.component_for_entity(attacker_id, Position)
                    start_pos = (blocker_pos.x + CARD_WIDTH / 2, blocker_pos.y + CARD_HEIGHT / 2)
                    end_pos = (attacker_pos.x + CARD_WIDTH / 2, attacker_pos.y + CARD_HEIGHT / 2)
                    pygame.draw.line(self.screen, (0, 255, 255), start_pos, end_pos, 5) # Cyan line for confirmed blocks

            # Draw phase overlay if needed
            if client_state.phase == GamePhase.COMBAT_DECLARE_BLOCKERS:
                overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
                overlay.fill((0, 0, 100, 64)) # Semi-transparent blue
                self.screen.blit(overlay, (0, 0))

            # Draw Player Info (Portraits) - это статичный UI, который рисуется каждый кадр
            if client_state.game_state_dict:
                my_player_data = client_state.game_state_dict.get("players", {}).get(str(client_state.my_player_id))
                if my_player_data:
                    self._draw_player_info(client_state.my_player_id)

                opp_id_str = next((pid for pid in client_state.game_state_dict.get("players", {}) if int(pid) != client_state.my_player_id), None)
                if opp_id_str:
                    self._draw_player_info(int(opp_id_str))

            # Draw Event Log
            self._draw_log(client_state)

            # Рисуем оверлей поверх всего, если оппонент отключился
            self._draw_connection_status_overlay(client_state)
            
            if client_state.current_animation:
                self._draw_animation(client_state.current_animation)

            # Draw all UI elements managed by the UIManager
            self.ui_manager.draw(self.screen)

        pygame.display.flip()

    def _draw_game_over_screen(self, client_state: ClientState):
        if client_state.winner_id == client_state.my_player_id:
            text = "ПОБЕДА!"
            color = (255, 215, 0)  # Gold
        else:
            text = "ПОРАЖЕНИЕ"
            color = (139, 0, 0)  # Dark Red

        text_surf = self.big_font.render(text, True, color)
        text_rect = text_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
        self.screen.blit(text_surf, text_rect)

    def _draw_animation(self, animation_event: Dict[str, Any]):
        """Draws a visual effect for the current animation event."""
        event_type = animation_event.get("type")
        payload = animation_event.get("payload", {})

        if event_type == "CARD_ATTACKED":
            attacker_id = payload.get("attacker_id")
            target_id = payload.get("target_id")

            # Helper to draw damage flash on cards
            def draw_damage_flash(card_id):
                if esper.entity_exists(card_id) and esper.has_component(card_id, Drawable):
                    drawable = esper.component_for_entity(card_id, Drawable)
                    overlay = pygame.Surface(drawable.sprite.rect.size, pygame.SRCALPHA)
                    overlay.fill((255, 0, 0, 100)) # semi-transparent red
                    self.screen.blit(overlay, drawable.sprite.rect.topleft)

            draw_damage_flash(attacker_id)
            draw_damage_flash(target_id)

            if esper.entity_exists(attacker_id) and esper.entity_exists(target_id):
                attacker_pos = esper.component_for_entity(attacker_id, Position)
                target_pos = esper.component_for_entity(target_id, Position)

                start_pos = (attacker_pos.x + CARD_WIDTH / 2, attacker_pos.y + CARD_HEIGHT / 2)
                end_pos = (target_pos.x + CARD_WIDTH / 2, target_pos.y + CARD_HEIGHT / 2)
                pygame.draw.line(self.screen, (255, 100, 0), start_pos, end_pos, 7)

                clash_text = self.emoji_font.render("💥", True, (255, 255, 0))
                clash_rect = clash_text.get_rect(center=((start_pos[0] + end_pos[0]) / 2, (start_pos[1] + end_pos[1]) / 2))
                self.screen.blit(clash_text, clash_rect)

        elif event_type == "PLAYER_DAMAGED":
            player_id = payload.get("player_id")
            portrait_rect = self._get_player_portrait_rect(player_id)
            if portrait_rect:
                overlay = pygame.Surface(portrait_rect.size, pygame.SRCALPHA)
                overlay.fill((255, 0, 0, 128))
                self.screen.blit(overlay, portrait_rect.topleft)

        elif event_type == "CARD_DIED":
            card_id = payload.get("card_id")
            # The entity still exists during this animation. It will be deleted by AnimationSystem when the timer expires.
            if esper.entity_exists(card_id) and esper.has_component(card_id, Position):
                card_pos = esper.component_for_entity(card_id, Position)
                skull_text = self.emoji_font.render("💀", True, (200, 200, 200))
                skull_rect = skull_text.get_rect(center=(card_pos.x + CARD_WIDTH / 2, card_pos.y + CARD_HEIGHT / 2))
                self.screen.blit(skull_text, skull_rect)

    def _draw_connection_status_overlay(self, client_state: ClientState):
        if not client_state.my_player_id or not client_state.player_connection_status:
            return

        opponent_id = next((pid for pid in client_state.player_connection_status if pid != client_state.my_player_id), None)

        if opponent_id is not None and client_state.player_connection_status.get(opponent_id) == "DISCONNECTED":
            # Рисуем полупрозрачный прямоугольник на весь экран
            overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 128))  # Черный, 50% прозрачности
            self.screen.blit(overlay, (0, 0))

            # Рисуем текст
            text = "Оппонент отключился. Ожидание..."
            text_surf = self.medium_font.render(text, True, (220, 220, 220))  # Светло-серый
            text_rect = text_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
            self.screen.blit(text_surf, text_rect)

    def _draw_log(self, client_state: ClientState):
        """Рисует лог событий в нижней части экрана."""
        log_rect = pygame.Rect(0, LOG_Y, SCREEN_WIDTH, LOG_HEIGHT)
        log_surface = pygame.Surface((SCREEN_WIDTH, LOG_HEIGHT), pygame.SRCALPHA)
        log_surface.fill((20, 20, 30, 200)) # Полупрозрачный фон

        # Рисуем сообщения снизу вверх, чтобы новые были выше
        for i, message in enumerate(reversed(client_state.log_messages)):
            if i >= LOG_LINES: break # Показываем только нужное количество строк
            # Небольшой отступ снизу для последней строки
            y_pos = LOG_HEIGHT - (i + 1) * LOG_LINE_HEIGHT + 2
            text_surf = self.log_font.render(message, True, (200, 200, 220))
            # Центрируем текст по горизонтали для лучшего вида
            text_rect = text_surf.get_rect(centerx=SCREEN_WIDTH / 2, y=y_pos)
            log_surface.blit(text_surf, text_rect)


        self.screen.blit(log_surface, log_rect.topleft)

    def _get_player_portrait_rect(self, player_id: int) -> Optional[pygame.Rect]:
        """Возвращает Rect для портрета игрока."""
        client_state = self.client_state
        if not client_state.game_state_dict or client_state.my_player_id is None:
            return None
        is_my_player = (player_id == client_state.my_player_id)
        y_pos = PLAYER_BOARD_Y if is_my_player else OPPONENT_BOARD_Y
        return pygame.Rect(PORTRAIT_X, y_pos, CARD_WIDTH, CARD_HEIGHT)

    def _draw_player_info(self, player_id: int):
        """Рисует портрет и информацию для указанного игрока."""
        player_data = self.client_state.game_state_dict.get("players", {}).get(str(player_id))
        if not player_data: return
        portrait_rect = self._get_player_portrait_rect(player_id)
        if not portrait_rect: return

        # Background and border
        pygame.draw.rect(self.screen, (30, 30, 40), portrait_rect)
        pygame.draw.rect(self.screen, (200, 200, 200), portrait_rect, 2)

        # Player info text (multiline)
        health = player_data.get('health', '?')
        mana_pool = player_data.get('mana_pool', '?')
        deck_size = player_data.get('deck_size', '?')
        graveyard_size = player_data.get('graveyard_size', '?')

        health_surf = self.font.render(f"Health: {health}", True, FONT_COLOR)
        mana_surf = self.font.render(f"Mana: {mana_pool}", True, FONT_COLOR)
        deck_surf = self.font.render(f"Deck: {deck_size}", True, FONT_COLOR)
        graveyard_surf = self.font.render(f"Grave: {graveyard_size}", True, FONT_COLOR)

        # Position them vertically inside the portrait
        self.screen.blit(health_surf, (portrait_rect.x + 10, portrait_rect.y + 15))
        self.screen.blit(mana_surf, (portrait_rect.x + 10, portrait_rect.y + 40))
        self.screen.blit(deck_surf, (portrait_rect.x + 10, portrait_rect.y + 65))
        self.screen.blit(graveyard_surf, (portrait_rect.x + 10, portrait_rect.y + 90))

# --- Сетевой поток (Network Thread) ---
# This class remains largely the same as it's a good pattern.
class NetworkThread(threading.Thread):
    """Поток для асинхронной работы с сетью, не блокируя Pygame."""
    def __init__(self, incoming_q: queue.Queue, outgoing_q: queue.Queue, host: str, port: int):
        super().__init__(daemon=True)
        self.incoming_q = incoming_q
        self.outgoing_q = outgoing_q
        self.host = host
        self.port = port
        self.loop = asyncio.new_event_loop()

    async def main_async(self):
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            self.incoming_q.put({"type": "CONNECTION_SUCCESS"})
            read_task = self.loop.create_task(self.read_from_server(reader))
            write_task = self.loop.create_task(self.write_to_server(writer))
            await asyncio.wait([read_task, write_task], return_when=asyncio.FIRST_COMPLETED)
        except ConnectionRefusedError:
            self.incoming_q.put({"type": "CONNECTION_FAILED"})
        finally:
            if self.loop.is_running():
                self.loop.stop()

    async def read_from_server(self, reader: asyncio.StreamReader):
        while True:
            data = await reader.readline()
            if not data:
                self.incoming_q.put({"type": "DISCONNECTED"})
                break
            try:
                self.incoming_q.put(json.loads(data.decode().strip()))
            except json.JSONDecodeError:
                print(f"Received non-JSON from server: {data.decode()}")

    async def write_to_server(self, writer: asyncio.StreamWriter):
        while True:
            command = await self.loop.run_in_executor(None, self.outgoing_q.get)
            if command is None: break
            writer.write((json.dumps(command) + '\n').encode())
            await writer.drain()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main_async())

# --- Основной класс клиента (Main Client Class) ---
class PygameClient:
    def __init__(self, host: str, port: int):
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Cardnet ECS Client")
        # --- Fonts ---
        self.font = pygame.font.Font(None, 24)
        self.medium_font = pygame.font.Font(None, 50)
        self.log_font = pygame.font.Font(None, 18)
        self.big_font = pygame.font.Font(None, 100)
        try:
            # Этот шрифт стандартен для Windows и поддерживает эмодзи.
            self.emoji_font = pygame.font.SysFont("Segoe UI Emoji", 50)
        except pygame.error:
            print("Warning: Segoe UI Emoji font not found. Falling back to default font for emojis.")
            # Если шрифт не найден, возвращаемся к стандартному, чтобы избежать сбоя.
            self.emoji_font = self.medium_font
        # --- State & Comms ---
        self.clock = pygame.time.Clock()
        self.client_state = ClientState()
        self.ui_manager = UIManager()
        self.running = True
        self.incoming_queue = queue.Queue()
        self.outgoing_queue = queue.Queue()
        self.network_thread = NetworkThread(self.incoming_queue, self.outgoing_queue, host, port)

    def run(self):
        # Instantiate systems that might depend on each other
        render_system = RenderSystem(self.screen, self.client_state, self.ui_manager,
                                     self.font, self.medium_font, self.log_font,
                                     self.big_font, self.emoji_font)
        ui_setup_system = UISetupSystem(self.client_state, self.ui_manager, self.font, self.medium_font)
        input_system = InputSystem(self.outgoing_queue, self.client_state, self.ui_manager)

        # Add systems to the world in the correct order for the game loop
        # State -> Layout -> UI Setup -> Input -> Animation -> Render
        esper.add_processor(StateUpdateSystem(self.incoming_queue, self.font, self.client_state))
        esper.add_processor(AnimationSystem(self.client_state))
        esper.add_processor(LayoutSystem(self.client_state))
        esper.add_processor(ui_setup_system)
        esper.add_processor(input_system)
        esper.add_processor(render_system)

        self.network_thread.start()


        while self.running:
            delta_time = self.clock.tick(60) / 1000.0

            # Check for exit signal
            if self.client_state.my_player_id == -999: # Сигнал выхода из InputSystem
                self.running = False
                continue

            esper.process(delta_time=delta_time)
        
        # Cleanup
        self.outgoing_queue.put(None) # Signal network thread to close
        self.network_thread.join(timeout=2)
        pygame.quit()
        sys.exit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cardnet: клиент для сетевой карточной игры.")
    parser.add_argument('--host', type=str, default='127.0.0.1',
                        help='IP-адрес сервера для подключения (по умолчанию: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8888,
                        help='Порт сервера для подключения (по умолчанию: 8888)')
    args = parser.parse_args()

    client = PygameClient(host=args.host, port=args.port)
    client.run()