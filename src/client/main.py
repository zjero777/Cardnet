"""
Клиент для многопользовательской карточной игры Cardnet.

--- РЕАЛИЗОВАНО ---
- Графический интерфейс на Pygame.
- Главное меню, браузер серверов в локальной сети.
- Интерактивное лобби с чатом и системой готовности.
- Асинхронное сетевое взаимодействие в отдельном потоке.
- Отображение игрового поля, карт в руке и на столе.
- ECS (esper) для управления состоянием на клиенте.
- Обработка базовых действий игрока (клик по карте, кнопкам).
- Обработка отключений и переподключений игроков.
- Система фаз хода (Главная 1, Атака, Блок, Главная 2).
- Базовые анимации (урон, уничтожение карты).
- UI-менеджер для кнопок и текстовых меток.
- Отображение статуса подключения и лога событий.

--- ПЛАН РАЗРАБОТКИ (TODO) ---
- Интерфейс для составления колод (Deck Builder).
- Улучшенные визуальные эффекты и анимации.
- Звуковые эффекты и музыкальное сопровождение.
- Улучшенный UX: подсказки для карт, более наглядная обратная связь.
"""
import asyncio
import json
import argparse
import socket
import sys
import threading
import queue
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum, auto

import time
import pygame
import esper

from .ui import (UIManager, Button, Label, TextInput, CONFIRM_BUTTON_COLOR, CONFIRM_BUTTON_HOVER_COLOR,
                 CONFIRM_BUTTON_PRESSED_COLOR, CONFIRM_BUTTON_TEXT_COLOR, TURN_INDICATOR_PLAYER_COLOR, TURN_INDICATOR_OPPONENT_COLOR, MENU_BUTTON_BG, MENU_BUTTON_HOVER, MENU_BUTTON_PRESSED, MENU_BUTTON_TEXT)

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

# --- Константы ---
CARD_BG_COLOR = (100, 100, 120)
CARD_HIGHLIGHT_COLOR = (255, 255, 0)
CARD_SELECTION_COLOR = (70, 170, 255)  # Светло-голубой для выделения
TARGET_COLOR = (255, 0, 0)
PUT_BOTTOM_COLOR = (255, 100, 255) # Малиновый для выбора карт для низа колоды
FONT_COLOR = (255, 255, 255)
PLAYER_COLOR = (100, 200, 100)
OPPONENT_COLOR = (200, 100, 100)
ATTACK_READY_COLOR = (0, 255, 0)

# --- Компоненты (Components) ---
BROADCAST_PORT = 8889


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
    network_status: str = "OFFLINE"  # OFFLINE, CONNECTING, CONNECTED, FAILED, DISCONNECTED
    game_phase: str = "MAIN_MENU"  # MAIN_MENU, SERVER_BROWSER, CONNECTING, LOBBY, MULLIGAN, GAME_RUNNING
    server_list: Dict[str, Dict] = field(default_factory=dict) # Discovered servers: {(ip, port): data}
    server_browser_enter_time: float = 0.0
    lobby_state: Dict[str, Any] = field(default_factory=dict) # State of player sessions in the lobby
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
    pending_put_bottom_cards: List[int] = field(default_factory=list)
    block_assignments: Dict[int, int] = field(default_factory=dict) # {blocker_id: attacker_id}
    # --- Animation State ---
    animation_queue: List[Dict] = field(default_factory=list)
    current_animation: Optional[Dict] = None
    animation_timer: float = 0.0
    # --- Event Log ---
    log_messages: List[str] = field(default_factory=list)
    max_log_messages: int = LOG_LINES
    chat_messages: List[Dict] = field(default_factory=list)
    max_chat_messages: int = 10

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

    def _draw_card_back(self):
        """Рисует рубашку карты."""
        self.image.fill((139, 69, 19))  # SaddleBrown
        pygame.draw.rect(self.image, (0, 0, 0), self.image.get_rect(), 5)
        pygame.draw.circle(self.image, (218, 165, 32), self.image.get_rect().center, 30)  # Goldenrod

    def _draw_card_face(self):
        """Рисует лицевую сторону карты: фон, имя, стоимость и характеристики."""
        self.image.fill(CARD_BG_COLOR)
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

    def _draw_status_overlays(self):
        """Рисует оверлеи и индикаторы статусов (болезнь вызова, поворот)."""
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

    def _draw_interaction_borders(self, is_hovered: bool, is_selected: bool):
        """Рисует рамки в зависимости от взаимодействия (атака, выбор, наведение)."""
        if self.card_data.get('is_attacking'):
            pygame.draw.rect(self.image, (255, 60, 60), self.image.get_rect(), 5)
        if is_selected:
            selection_color = PUT_BOTTOM_COLOR if self.card_data.get('is_pending_put_bottom', False) else CARD_SELECTION_COLOR
            pygame.draw.rect(self.image, selection_color, self.image.get_rect(), 5)
        if is_hovered:
            pygame.draw.rect(self.image, CARD_HIGHLIGHT_COLOR, self.image.get_rect(), 4)

    def update_visuals(self, is_hovered: bool = False, is_selected: bool = False):
        """Перерисовывает внешний вид карты на основе ее данных."""
        if self.card_data.get('is_hidden', False):
            self._draw_card_back()
        else:
            self._draw_card_face()
            self._draw_status_overlays()

        # Рамки рисуются поверх всего остального
        self._draw_interaction_borders(is_hovered, is_selected)


class ServerDiscoveryThread(threading.Thread):
    """Поток для обнаружения серверов в локальной сети по UDP broadcast."""
    def __init__(self, discovery_q: queue.Queue):
        super().__init__(daemon=True)
        self.discovery_q = discovery_q
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR позволяет нескольким клиентам на одной машине слушать broadcast
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('', BROADCAST_PORT))
        except OSError as e:
            # Адрес уже используется, возможно другой клиент запущен
            self.discovery_q.put({"type": "DISCOVERY_ERROR", "payload": {"message": f"Could not bind to port {BROADCAST_PORT}: {e}"}})
            return
        
        sock.settimeout(1.0) # Таймаут, чтобы проверять self.running
        
        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                server_info = json.loads(data.decode('utf-8'))
                server_info['ip'] = addr[0]
                self.discovery_q.put({"type": "SERVER_FOUND", "payload": server_info})
            except socket.timeout:
                continue
            except (json.JSONDecodeError, KeyError):
                # Игнорируем некорректные пакеты
                continue
            except Exception as e:
                print(f"Error in discovery thread: {e}")
        
        sock.close()
# --- Системы (Systems) ---

class StateUpdateSystem(esper.Processor):
    """Processes messages from the server and updates the client's ECS world."""
    def __init__(self, incoming_q: queue.Queue, discovery_q: queue.Queue, font: pygame.font.Font, client_state: ClientState):
        self.incoming_q = incoming_q
        self.discovery_q = discovery_q # new
        self.font = font
        self.client_state = client_state
        self.server_timeout = 15.0 # seconds

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
            # NEW: Process discovery queue
            while True:
                event = self.discovery_q.get_nowait()
                event_type = event.get("type")

                if event_type == "SERVER_FOUND":
                    server_info = event["payload"]
                    server_key = (server_info['ip'], server_info['tcp_port'])
                    server_info['last_seen'] = time.time()
                    self.client_state.server_list[server_key] = server_info
                elif event_type == "DISCOVERY_ERROR":
                    # Maybe show this error to the user
                    print(f"Discovery Error: {event['payload']['message']}")

        except queue.Empty:
            pass

        # NEW: Prune stale servers from the list
        now = time.time()
        stale_keys = [
            key for key, info in self.client_state.server_list.items()
            if now - info.get('last_seen', 0) > self.server_timeout
        ]
        for key in stale_keys:
            del self.client_state.server_list[key]
        try:
            while True:
                event = self.incoming_q.get_nowait()
                event_type = event.get("type")

                if event_type == "ASSIGN_PLAYER_ID":
                    self.client_state.my_player_id = event["payload"]["player_id"]

                elif event_type == "CONNECTION_SUCCESS":
                    self.client_state.network_status = "CONNECTED"
                    self.client_state.game_phase = "LOBBY"

                elif event_type == "LOBBY_UPDATE":
                    # Server sent an update while we are in the lobby
                    self.client_state.game_phase = "LOBBY"
                    self.client_state.lobby_state = event.get("payload", {}).get("sessions", {})

                elif event_type == "FULL_STATE_UPDATE":
                    game_state_dict = event.get("payload", {})
                    self.client_state.game_state_dict = game_state_dict
                    self.client_state.active_player_id = game_state_dict.get("active_player_id")
                    # If we get a state update, we must be connected.
                    self.client_state.network_status = "CONNECTED"
                    # Полное обновление состояния синхронизирует мир, но не должно менять текущую фазу хода.
                    # Фаза меняется только специальными событиями (TURN_STARTED, COMBAT_RESOLVED и т.д.),
                    
                    # NEW: Update game phase
                    new_phase = game_state_dict.get("game_phase", "UNKNOWN")
                    if new_phase != self.client_state.game_phase:
                        print(f"--- Game phase changed to: {new_phase} ---")
                        self.client_state.game_phase = new_phase
                        self.client_state.pending_put_bottom_cards.clear()

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

                elif event_type == "CONNECTION_FAILED":
                    reason = event.get("payload", {}).get("reason", "Unknown error")
                    print(f"Network status: CONNECTION_FAILED. Reason: {reason}")
                    self.client_state.network_status = "FAILED"
                elif event_type == "DISCONNECTED":
                    print(f"Network status: DISCONNECTED")
                    self.client_state.network_status = "DISCONNECTED"
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
                elif event_type == "CHAT_MESSAGE":
                    payload = event.get("payload", {})
                    sender_id = payload.get("sender_id")
                    text = payload.get("text")
                    sender_name = f"Игрок {sender_id}"
                    if sender_id == self.client_state.my_player_id:
                        sender_name = "Вы"

                    self.client_state.chat_messages.append({"sender": sender_name, "text": text})
                    if len(self.client_state.chat_messages) > self.client_state.max_chat_messages:
                        self.client_state.chat_messages.pop(0)
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
    def __init__(self, client_state: ClientState, ui_manager: UIManager, font: pygame.font.Font, medium_font: pygame.font.Font, start_connection_callback, reset_to_menu_callback, disconnect_callback, chat_input_ref):
        self.client_state = client_state
        self.ui_manager = ui_manager
        self.font = font
        self.medium_font = medium_font
        self.start_connection = start_connection_callback
        self.reset_to_menu = reset_to_menu_callback
        self.disconnect_and_go_back = disconnect_callback
        self.chat_input = chat_input_ref

    def process(self, *args, **kwargs):
        # Clear UI from the previous frame
        self.ui_manager.clear_elements()

        if self.client_state.game_phase == "MAIN_MENU":
            self._setup_main_menu_ui()
            return

        if self.client_state.game_phase == "SERVER_BROWSER": # NEW
            self._setup_server_browser_ui()
            return

        if self.client_state.network_status in ["FAILED", "DISCONNECTED"]:
            self._setup_error_ui()
            return

        # Если оппонент отключен, не показываем никаких интерактивных элементов.
        # Оверлей будет нарисован RenderSystem.
        opponent_id = next((pid for pid in self.client_state.player_connection_status if pid != self.client_state.my_player_id), None)
        if opponent_id is not None and self.client_state.player_connection_status.get(opponent_id) == "DISCONNECTED":
            # Не создаем никаких кнопок.
            return

        if self.client_state.game_phase == "CONNECTING":
            # No UI elements while connecting, RenderSystem shows a message
            pass
        elif self.client_state.game_phase == "LOBBY":
            self._setup_lobby_ui()
        elif self.client_state.game_phase == "MULLIGAN":
            self._setup_mulligan_ui(self.client_state)
        elif not self.client_state.game_over and self.client_state.game_state_dict:
            self._setup_ui(self.client_state)

    def _setup_main_menu_ui(self):
        """Создает кнопки для главного меню."""
        center_x = SCREEN_WIDTH // 2

        # Располагаем кнопки ниже центра
        button_y_start = SCREEN_HEIGHT * 0.5
        button_width = 350
        button_height = 60
        button_spacing = 30

        join_button = Button(
            "Присоединиться к игре",
            pygame.Rect(center_x - button_width // 2, button_y_start, button_width, button_height),
            self.font,
            lambda: setattr(self.client_state, 'game_phase', 'SERVER_BROWSER'),
            bg_color=MENU_BUTTON_BG, hover_color=MENU_BUTTON_HOVER, pressed_color=MENU_BUTTON_PRESSED, text_color=MENU_BUTTON_TEXT
        )
        quit_button = Button(
            "Выход",
            pygame.Rect(center_x - button_width // 2, button_y_start + button_height + button_spacing, button_width, button_height),
            self.font,
            lambda: setattr(self.client_state, 'my_player_id', -999),
            bg_color=MENU_BUTTON_BG, hover_color=MENU_BUTTON_HOVER, pressed_color=MENU_BUTTON_PRESSED, text_color=MENU_BUTTON_TEXT
        )
        self.ui_manager.add_element(join_button)
        self.ui_manager.add_element(quit_button)

    def _setup_error_ui(self):
        """Создает кнопку "Назад в меню" на экране ошибки."""
        center_x, center_y = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 50
        back_button = Button("В меню", pygame.Rect(center_x - 100, center_y, 200, 50), self.font, self.reset_to_menu)
        self.ui_manager.add_element(back_button)

    def _setup_server_browser_ui(self): # NEW
        """Создает UI для списка серверов."""
        # Кнопка "Назад"
        back_button = Button("Назад", pygame.Rect(20, SCREEN_HEIGHT - 70, 150, 50), self.font, self.reset_to_menu)
        self.ui_manager.add_element(back_button)

        # Кнопки для каждого найденного сервера
        y_pos = SCREEN_HEIGHT * 0.3 # Начинаем ниже, чтобы освободить место для заголовка
        for (ip, port), server_info in sorted(self.client_state.server_list.items()):
            server_name = server_info.get('server_name', 'Unknown Server')
            players = server_info.get('players', '?/?')
            status = server_info.get('status', 'UNKNOWN')
            
            button_text = f"{server_name} - {players} - {status} ({ip}:{port})"
            
            def make_callback(h, p):
                return lambda: self.start_connection(h, p)

            server_button = Button(button_text, pygame.Rect(SCREEN_WIDTH // 2 - 300, y_pos, 600, 40), self.font, make_callback(ip, port))
            self.ui_manager.add_element(server_button)
            y_pos += 50

    def _setup_lobby_ui(self):
        """Создает UI для лобби, включая кнопку готовности."""
        my_id = self.client_state.my_player_id
        if my_id is None:
            return

        my_session_data = self.client_state.lobby_state.get(str(my_id))
        if not my_session_data:
            return

        # Если игрок еще не готов, показываем кнопку "Готов"
        if not my_session_data.get("ready", False):
            input_system = esper.get_processor(InputSystem)
            def ready_callback():
                input_system.outgoing_q.put({"type": "PLAYER_READY"})

            ready_button = Button(
                "Готов",
                pygame.Rect(SCREEN_WIDTH - 150 - 20, SCREEN_HEIGHT - 70, 150, 50),
                self.font,
                ready_callback,
                bg_color=CONFIRM_BUTTON_COLOR,
                hover_color=CONFIRM_BUTTON_HOVER_COLOR,
                pressed_color=CONFIRM_BUTTON_PRESSED_COLOR,
                text_color=CONFIRM_BUTTON_TEXT_COLOR
            )
            self.ui_manager.add_element(ready_button)

        # Добавляем поле ввода чата в UI менеджер, чтобы оно отрисовалось
        self.ui_manager.add_element(self.chat_input)

        back_button = Button(
            "Назад",
            pygame.Rect(20, SCREEN_HEIGHT - 70, 150, 50),
            self.font,
            self.disconnect_and_go_back,
            bg_color=MENU_BUTTON_BG, hover_color=MENU_BUTTON_HOVER, pressed_color=MENU_BUTTON_PRESSED, text_color=MENU_BUTTON_TEXT
        )
        self.ui_manager.add_element(back_button)

    def _setup_mulligan_ui(self, client_state: ClientState):
        if not client_state.game_state_dict or client_state.my_player_id is None:
            return

        my_player_data = client_state.game_state_dict.get("players", {}).get(str(client_state.my_player_id))
        if not my_player_data:
            return

        my_mulligan_state = my_player_data.get("mulligan_state", "NONE")
        input_system = esper.get_processor(InputSystem)

        center_x, center_y = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2

        if my_mulligan_state == "DECIDING":
            # Показываем кнопки "Оставить" и "Муллиган"
            keep_button = Button("Keep Hand", pygame.Rect(center_x - 220, center_y - 25, 200, 50), self.font,
                                 lambda: input_system.outgoing_q.put({"type": "KEEP_HAND"}))
            mulligan_button = Button("Mulligan", pygame.Rect(center_x + 20, center_y - 25, 200, 50), self.font,
                                     lambda: input_system.outgoing_q.put({"type": "MULLIGAN"}))
            self.ui_manager.add_element(keep_button)
            self.ui_manager.add_element(mulligan_button)

        elif my_mulligan_state == "PUT_BOTTOM":
            count = my_player_data.get("mulligan_put_bottom_count", 0)
            label_text = f"Select {count} card(s) to put on the bottom of your library."
            label = Label(label_text, (center_x, center_y - 50), self.font, (255, 255, 255), center=True)
            self.ui_manager.add_element(label)

            # Кнопка подтверждения активна, только если выбрано нужное количество карт
            num_selected = len(client_state.pending_put_bottom_cards)
            if num_selected == count:
                def confirm_callback():
                    # Отправляем копию списка, чтобы избежать race condition с сетевым потоком.
                    cards_to_put_bottom = list(client_state.pending_put_bottom_cards)
                    input_system.outgoing_q.put({
                        "type": "PUT_CARDS_BOTTOM",
                        "payload": {"card_ids": cards_to_put_bottom}
                    })
                    # Сразу очищаем выбор на клиенте для отзывчивости интерфейса.
                    client_state.pending_put_bottom_cards.clear()

                confirm_button = Button("Confirm", pygame.Rect(center_x - 100, center_y, 200, 50), self.font, confirm_callback)
                self.ui_manager.add_element(confirm_button)

        elif my_mulligan_state == "WAITING":
            label = Label("Waiting for opponent to decide...", (center_x, center_y), self.medium_font, (200, 200, 200), center=True)
            self.ui_manager.add_element(label)

    def _setup_ui(self, client_state: ClientState):
        """Creates and adds all necessary UI elements to the UIManager for the current frame based on game state."""
        # Эта функция теперь вызывается только когда игра идет (не в фазе муллигана)
        if client_state.active_player_id is None or client_state.game_phase != "GAME_RUNNING":
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
    def __init__(self, outgoing_q: queue.Queue, client_state: ClientState, ui_manager: UIManager, chat_input_ref: TextInput):
        self.outgoing_q = outgoing_q
        self.client_state = client_state
        self.ui_manager = ui_manager
        self.chat_input = chat_input_ref

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

        # Определяем, нужно ли блокировать ввод из-за отключения оппонента.
        opponent_id = next((pid for pid in client_state.player_connection_status if pid != client_state.my_player_id), None)
        is_opponent_disconnected = (opponent_id is not None and client_state.player_connection_status.get(opponent_id) == "DISCONNECTED")

        # Обновляем информацию о наведении мыши каждый кадр, а не только по событию
        # Только если игра интерактивна
        if not is_opponent_disconnected:
            self._handle_mouse_motion(pygame.mouse.get_pos(), client_state)

        for event in pygame.event.get():
            # Событие выхода обрабатывается всегда
            if event.type == pygame.QUIT:
                self.outgoing_q.put(None) # Signal network thread to close
                client_state.my_player_id = -999 # Сигнал для выхода из главного цикла
                return

            # Если оппонент отключен, игнорируем все остальные события ввода
            if is_opponent_disconnected:
                continue

            # Let the UI Manager process the event first. If it handles it, we skip the game logic for this event.
            if self.ui_manager.process_event(event):
                continue

            # Если соединение не удалось или разорвано, ждем любого ввода для выхода
            if client_state.network_status in ["FAILED", "DISCONNECTED"]:
                if event.type in [pygame.QUIT, pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN]:
                    self.outgoing_q.put(None)
                    client_state.my_player_id = -999 # Сигнал для выхода
                continue # Не обрабатываем другие события

            # Если игра окончена, игнорируем все остальные события ввода
            if client_state.game_over:
                continue
            
            # Обработка ввода для чата в лобби
            if client_state.game_phase == "LOBBY":
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.chat_input.is_active = self.chat_input.rect.collidepoint(event.pos)
                
                if self.chat_input.is_active and event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_RETURN:
                        if self.chat_input.text:
                            self.outgoing_q.put({"type": "CHAT_MESSAGE", "payload": {"text": self.chat_input.text}})
                            self.chat_input.text = ""
                    elif event.key == pygame.K_BACKSPACE:
                        self.chat_input.text = self.chat_input.text[:-1]
                    elif len(self.chat_input.text) < self.chat_input.max_len:
                        self.chat_input.text += event.unicode

            # NEW: Mulligan phase input
            if client_state.game_phase == "MULLIGAN":
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self._handle_put_bottom_click(event.pos, client_state)
                continue # Больше никакой ввод не обрабатывается в этой фазе

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
        # Добавляем проверку: обрабатываем клики по игровым объектам (карты, портреты)
        # только тогда, когда игра находится в активной фазе.
        if client_state.game_phase != "GAME_RUNNING":
            return

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
                    client_state.pending_attackers.remove(clicked_entity)
                else:
                    client_state.pending_attackers.append(clicked_entity)

    def _handle_put_bottom_click(self, pos, client_state: ClientState):
        """Обрабатывает клики для выбора карт для низа колоды во время муллигана."""
        my_player_data = client_state.game_state_dict.get("players", {}).get(str(client_state.my_player_id))
        if not my_player_data or my_player_data.get("mulligan_state") != "PUT_BOTTOM":
            return

        # Обновляем, на какую карту наведен курсор, а затем используем это состояние.
        self._handle_mouse_motion(pos, client_state)
        clicked_card_entity = client_state.hovered_entity

        if not clicked_card_entity:
            return

        drawable = esper.component_for_entity(clicked_card_entity, Drawable)
        card_data = drawable.sprite.card_data
        is_my_card_in_hand = (card_data.get("owner_id") == client_state.my_player_id and
                              card_data.get("location") == "HAND")

        if not is_my_card_in_hand:
            return

        if clicked_card_entity in client_state.pending_put_bottom_cards:
            client_state.pending_put_bottom_cards.remove(clicked_card_entity)
        else:
            # Проверяем, можно ли выбрать еще карты
            count_to_put = my_player_data.get("mulligan_put_bottom_count", 0)
            if len(client_state.pending_put_bottom_cards) < count_to_put:
                client_state.pending_put_bottom_cards.append(clicked_card_entity)
 
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
        self.font = font # Regular font
        self.medium_font = medium_font
        self.log_font = log_font
        self.big_font = big_font
        self.emoji_font = emoji_font
        # Сделаем шрифт для заголовка крупнее
        self.title_font = self.big_font

    def process(self, *args, **kwargs):
        client_state = self.client_state
        self.screen.fill(BG_COLOR)

        if client_state.game_phase == "MAIN_MENU":
            self._draw_main_menu_screen()
        elif client_state.game_phase == "SERVER_BROWSER": # NEW
            self._draw_server_browser_screen()
        elif client_state.network_status == "CONNECTING":
            self._draw_message_screen("Подключение к серверу...", (200, 200, 200))
        elif client_state.network_status in ["FAILED", "DISCONNECTED"]:
            reason = "Не удалось подключиться к серверу."
            if client_state.network_status == "DISCONNECTED":
                reason = "Соединение с сервером потеряно."
            self._draw_message_screen(f"{reason}\nНажмите любую клавишу для выхода.", (255, 100, 100))
        elif client_state.game_phase == "LOBBY":
            self._draw_lobby_screen(client_state)
        elif client_state.game_over:
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
                # Добавляем флаг для отрисовки рамки муллигана
                drawable.sprite.card_data['is_pending_put_bottom'] = (client_state.game_phase == "MULLIGAN" and ent in client_state.pending_put_bottom_cards)

                # Если это карта под курсором, откладываем ее отрисовку
                if ent == hovered_entity_id:
                    hovered_card_to_draw = (drawable, pos)
                    continue

                is_selected = (ent == client_state.selected_entity or
                               ent in client_state.pending_put_bottom_cards or
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
                               hovered_entity_id in client_state.pending_put_bottom_cards or
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

        # UI рисуется поверх всего, даже на экранах сообщений
        self.ui_manager.draw(self.screen)
        pygame.display.flip()

    def _draw_server_browser_screen(self): # NEW
        """Рисует экран списка серверов."""
        if self.client_state.server_list:
            title_text = "Выберите сервер"
            title_color = (255, 255, 255)
        else:
            title_text = "Поиск серверов..."
            title_color = (200, 200, 200)

        title_surf = self.medium_font.render(title_text, True, title_color)
        title_rect = title_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT * 0.15))
        self.screen.blit(title_surf, title_rect)

        # Показываем сообщение "серверы не найдены" только после небольшой задержки,
        # чтобы дать время на их обнаружение.
        # Сервер рассылает broadcast каждые 5 секунд, поэтому ждем чуть больше двух циклов.
        SEARCH_TIMEOUT = 11.0 # seconds
        time_since_search_started = time.time() - self.client_state.server_browser_enter_time

        if not self.client_state.server_list and time_since_search_started > SEARCH_TIMEOUT:
            no_servers_text = self.font.render("Серверы не найдены. Убедитесь, что сервер запущен в вашей сети.", True, (200, 200, 200))
            text_rect = no_servers_text.get_rect(centerx=SCREEN_WIDTH // 2, y=SCREEN_HEIGHT // 2)
            self.screen.blit(no_servers_text, text_rect)

    def _draw_main_menu_screen(self):
        # Рисуем заголовок вверху экрана
        title_surf = self.title_font.render("Cardnet", True, (255, 215, 0))
        title_rect = title_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT * 0.2))
        self.screen.blit(title_surf, title_rect)

    def _draw_lobby_screen(self, client_state: ClientState):
        """Рисует экран лобби в ожидании игроков."""
        title_surf = self.title_font.render("Лобби", True, (255, 215, 0))
        title_rect = title_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT * 0.1))
        self.screen.blit(title_surf, title_rect)

        lobby_state = client_state.lobby_state
        if not lobby_state:
            return

        # --- Отрисовка списка игроков ---
        y_start = SCREEN_HEIGHT * 0.25

        for player_id_str, session_data in sorted(lobby_state.items()):
            player_id = int(player_id_str)
            status = session_data.get("status", "UNKNOWN")
            is_ready = session_data.get("ready", False)

            ready_text = " (Готов)" if is_ready else " (Не готов)"
            color = (100, 255, 100) if is_ready else (255, 200, 100)
            if status != "CONNECTED":
                color = (255, 100, 100)
                ready_text = ""

            text = f"Игрок {player_id}: {status}{ready_text}"
            
            if player_id == client_state.my_player_id:
                text += " (Вы)"

            text_surf = self.medium_font.render(text, True, color)
            text_rect = text_surf.get_rect(centerx=SCREEN_WIDTH // 2, y=y_start)
            self.screen.blit(text_surf, text_rect)
            y_start += self.medium_font.get_height() + 10

        # --- Отрисовка чата ---
        chat_log_rect = pygame.Rect(200, SCREEN_HEIGHT - 200, SCREEN_WIDTH - 400, 150)
        pygame.draw.rect(self.screen, (20, 20, 25, 180), chat_log_rect, border_radius=5)
        
        chat_y = chat_log_rect.bottom - 25
        for msg in reversed(client_state.chat_messages):
            sender_text = f"<{msg['sender']}> "
            message_text = msg['text']

            sender_color = (255, 215, 0)  # Gold for sender
            message_color = (240, 240, 240) # Brighter white for message

            sender_surf = self.font.render(sender_text, True, sender_color)
            message_surf = self.font.render(message_text, True, message_color)

            self.screen.blit(sender_surf, (chat_log_rect.x + 10, chat_y))
            self.screen.blit(message_surf, (chat_log_rect.x + 10 + sender_surf.get_width(), chat_y))

            chat_y -= self.font.get_height() + 2
            if chat_y < chat_log_rect.top:
                break

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

    def _draw_message_screen(self, text: str, color: tuple, font: Optional[pygame.font.Font] = None):
        """Вспомогательная функция для отрисовки центрированного сообщения на экране."""
        font_to_use = font or self.medium_font
        lines = text.split('\n')
        total_height = len(lines) * font_to_use.get_height()
        start_y = (SCREEN_HEIGHT - total_height) // 2

        for i, line in enumerate(lines):
            text_surf = font_to_use.render(line, True, color)
            text_rect = text_surf.get_rect( #
                centerx=SCREEN_WIDTH // 2, 
                y=start_y + i * font_to_use.get_height())
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
            # Добавляем таймаут для попытки подключения
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0
            )
            self.incoming_q.put({"type": "CONNECTION_SUCCESS"})
            read_task = self.loop.create_task(self.read_from_server(reader))
            write_task = self.loop.create_task(self.write_to_server(writer))
            await asyncio.wait([read_task, write_task], return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            self.incoming_q.put({"type": "CONNECTION_FAILED", "payload": {"reason": str(e)}})
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
        self.discovery_queue = queue.Queue()
        self.host = host
        self.port = port
        self.network_thread = None # Будет создан при подключении
        self.discovery_thread = None
        self.chat_input = TextInput(
            rect=pygame.Rect(200, SCREEN_HEIGHT - 45, SCREEN_WIDTH - 400, 35),
            font=self.font,
            text_color=MENU_BUTTON_TEXT
        )

    def start_discovery(self):
        """Инициирует поток для поиска серверов."""
        if self.discovery_thread and self.discovery_thread.is_alive():
            return
        self.discovery_thread = ServerDiscoveryThread(self.discovery_queue)
        self.discovery_thread.start()

    def stop_discovery(self):
        """Останавливает поток поиска серверов."""
        if self.discovery_thread:
            self.discovery_thread.stop()
            self.discovery_thread.join(timeout=0.2)
            self.discovery_thread = None
        # Очищаем список серверов, когда прекращаем поиск
        self.client_state.server_list.clear()

    def start_connection(self, host, port):
        """Инициирует новое подключение к серверу."""
        if self.network_thread and self.network_thread.is_alive():
            return # Уже идет попытка подключения или подключено

        self.stop_discovery()  # Останавливаем поиск серверов при попытке подключения
        self.client_state.network_status = "CONNECTING"
        self.client_state.game_phase = "CONNECTING"

        self.network_thread = NetworkThread(self.incoming_queue, self.outgoing_queue, host, port)
        self.network_thread.start()

    def disconnect_and_go_to_server_browser(self):
        """Disconnects from the current server and returns to the server browser."""
        if self.network_thread and self.network_thread.is_alive():
            self.outgoing_queue.put(None)
            self.network_thread.join(timeout=0.2)
        self.network_thread = None

        cs = self.client_state
        cs.my_player_id = None
        cs.active_player_id = None
        cs.game_state_dict = None
        cs.network_status = "OFFLINE"
        cs.game_phase = "SERVER_BROWSER"
        # Don't clear server_list
        cs.server_browser_enter_time = 0.0
        cs.lobby_state.clear()
        cs.selected_entity = None
        cs.hovered_entity = None
        cs.game_over = False
        cs.winner_id = None
        cs.player_connection_status.clear()
        cs.phase = GamePhase.MAIN_1
        cs.attackers.clear()
        cs.pending_attackers.clear()
        cs.selected_blocker = None
        cs.pending_put_bottom_cards.clear()
        cs.block_assignments.clear()

    def reset_to_menu(self):
        """Сбрасывает состояние клиента в главное меню."""
        self.stop_discovery()
        if self.network_thread and self.network_thread.is_alive():
            # Поток должен был умереть сам при ошибке, но на всякий случай
            self.network_thread.join(timeout=0.1)

        self.network_thread = None
        # Reset the existing ClientState object instead of creating a new one.
        # This ensures all systems that hold a reference to it see the changes.
        cs = self.client_state
        cs.my_player_id = None
        cs.active_player_id = None
        cs.game_state_dict = None
        cs.network_status = "OFFLINE"
        cs.game_phase = "MAIN_MENU"
        cs.server_list.clear()
        cs.server_browser_enter_time = 0.0
        cs.lobby_state.clear()
        cs.selected_entity = None
        cs.hovered_entity = None
        cs.game_over = False
        cs.winner_id = None
        cs.player_connection_status.clear()
        cs.phase = GamePhase.MAIN_1
        cs.attackers.clear()
        cs.pending_attackers.clear()
        cs.selected_blocker = None
        cs.pending_put_bottom_cards.clear()
        cs.block_assignments.clear()
        cs.animation_queue.clear()
        cs.current_animation = None
        cs.animation_timer = 0.0
        cs.log_messages.clear()
        # Очищаем очереди на случай, если там что-то осталось
        while not self.incoming_queue.empty(): self.incoming_queue.get_nowait()
        while not self.outgoing_queue.empty(): self.outgoing_queue.get_nowait()
        while not self.discovery_queue.empty(): self.discovery_queue.get_nowait()

    def run(self):
        # Instantiate systems that might depend on each other
        render_system = RenderSystem(self.screen, self.client_state, self.ui_manager,
                                     self.font, self.medium_font, self.log_font,
                                     self.big_font, self.emoji_font)
        # Передаем колбэки для управления подключением
        ui_setup_system = UISetupSystem(self.client_state, self.ui_manager, self.font, self.medium_font,
                                        self.start_connection, self.reset_to_menu, self.disconnect_and_go_to_server_browser, self.chat_input)
        input_system = InputSystem(self.outgoing_queue, self.client_state, self.ui_manager, self.chat_input)

        # Add systems to the world in the correct order for the game loop
        # State -> Layout -> UI Setup -> Input -> Animation -> Render
        esper.add_processor(StateUpdateSystem(self.incoming_queue, self.discovery_queue, self.font, self.client_state))
        esper.add_processor(AnimationSystem(self.client_state))
        esper.add_processor(LayoutSystem(self.client_state))
        esper.add_processor(ui_setup_system)
        esper.add_processor(input_system)
        esper.add_processor(render_system)

        while self.running:
            delta_time = self.clock.tick(60) / 1000.0
            self.chat_input.update(delta_time)

            # Check for exit signal
            if self.client_state.my_player_id == -999: # Сигнал выхода из InputSystem
                self.running = False
                continue

            # NEW: Manage discovery thread based on game phase
            if self.client_state.game_phase == 'SERVER_BROWSER' and (not self.discovery_thread or not self.discovery_thread.is_alive()):
                # Запускаем поток поиска и засекаем время входа в этот режим
                if self.client_state.server_browser_enter_time == 0.0:
                    self.client_state.server_browser_enter_time = time.time()
                self.start_discovery()
            elif self.client_state.game_phase != 'SERVER_BROWSER' and self.discovery_thread and self.discovery_thread.is_alive():
                # Сбрасываем таймер при выходе из режима поиска
                self.client_state.server_browser_enter_time = 0.0
                self.stop_discovery()

            esper.process(delta_time=delta_time)
        
        # Cleanup
        self.stop_discovery() # NEW
        if self.network_thread and self.network_thread.is_alive():
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