"""
Сервер для многопользовательской карточной игры Cardnet.

--- РЕАЛИЗОВАНО ---
- Асинхронный TCP-сервер на asyncio.
- Поддержка двух игроков с переподключением.
- Игровая логика на базе ECS (esper).
- Базовые системы: розыгрыш карт, смена хода, атака, конец игры.
- Правило "муллигана" (сброс стартовой руки) по правилам MTG.
- Создание колод, стартовая рука, базовые типы карт (существа, земли, заклинания).
- Сериализация и отправка состояния игры клиентам.
- Обработка команд от клиентов.

--- ПЛАН РАЗРАБОТКИ (TODO) ---
- Более сложные механики карт (триггеры, пассивные способности).
- Расширенная система фаз (Upkeep, End Step).
- Загрузка колод из файлов.
- Лобби и система подбора игроков.
- Улучшенная валидация действий игроков на сервере.
"""
import asyncio
import json
import logging
import random
from typing import Any
import time
import esper
from src.common.components import (
    CardInfo, Player, Owner, InHand, OnBoard, PlayCardCommand, GameOver, SpellEffect, ActiveTurn, Graveyard,
    InGraveyard, EndTurnCommand, Deck, InDeck, Tapped, TapLandCommand, DeclareAttackersCommand,
    Attacking, SummoningSickness, DeclareBlockersCommand,
    MulliganCommand, KeepHandCommand, PutCardsBottomCommand, MulliganDecisionPhase, MulliganCount, GamePhaseComponent, KeptHand
)
from src.server.systems import (PlayCardSystem, TurnManagementSystem, AttackSystem, GameOverSystem, TapLandSystem,
                                MulliganSystem)

# --- Глобальное состояние ---
# Глобальное состояние для сессий игроков. Позволяет обрабатывать переподключения.
player_sessions = {
    1: {"writer": None, "status": "DISCONNECTED", "addr": None},
    2: {"writer": None, "status": "DISCONNECTED", "addr": None}
}
# Очередь событий для отправки клиентам. Системы будут добавлять сюда события.
event_queue = []


# --- Сетевая логика ---

async def broadcast(message: dict):
    """Отправляет сообщение всем подключенным клиентам."""
    encoded_message = (json.dumps(message) + '\n').encode()
    # Собираем всех активных "писателей" из сессий
    current_writers = [
        session["writer"]
        for session in player_sessions.values()
        if session["status"] == "CONNECTED" and session["writer"] is not None
    ]

    for writer in current_writers:
        try:
            writer.write(encoded_message)
            # Ждем, пока буфер не освободится, но не дольше 1 секунды
            await asyncio.wait_for(writer.drain(), timeout=1.0)
        except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError) as e:
            # Этот клиент не отвечает или отключился. Логируем и идем дальше.
            # В будущем здесь можно будет инициировать его принудительное отключение.
            addr = writer.get_extra_info('peername', '???')
            logging.warning(f"Could not send message to client {addr}: {e}")


async def send_to_one(writer: asyncio.StreamWriter, message: dict):
    """Отправляет сообщение одному конкретному клиенту."""
    try:
        encoded_message = (json.dumps(message) + '\n').encode()
        writer.write(encoded_message)
        # Добавляем таймаут, чтобы один "зависший" клиент не блокировал сервер
        await asyncio.wait_for(writer.drain(), timeout=5.0)
    except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError) as e:
        addr = writer.get_extra_info('peername', '???')
        logging.warning(f"Could not send message to client {addr}: {e}")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Эта корутина выполняется для каждого нового подключения клиента."""
    addr = writer.get_extra_info('peername', '???')
    logging.info(f"New client trying to connect: {addr}")

    player_entity_id = None
    is_reconnect = False
    # Ищем свободный или отключенный слот для игрока
    for p_id, session in player_sessions.items():
        if session["status"] == "DISCONNECTED":
            player_entity_id = p_id
            # Если у слота уже был адрес, значит это переподключение, а не первая инициализация
            if session["addr"] is not None:
                is_reconnect = True
            break

    if player_entity_id is None:
        logging.warning(f"Connection rejected for {addr}: all slots are full.")
        await send_to_one(writer, {"type": "GAME_FULL"})
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return

    # Обновляем сессию для подключившегося игрока
    player_sessions[player_entity_id]["writer"] = writer
    player_sessions[player_entity_id]["status"] = "CONNECTED"
    player_sessions[player_entity_id]["addr"] = addr
    logging.info(f"Client {addr} assigned to player ID {player_entity_id}. Status: CONNECTED/RECONNECTED")

    # Если это было переподключение, уведомляем всех
    if is_reconnect:
        await broadcast({"type": "PLAYER_RECONNECTED", "payload": {"player_id": player_entity_id}})

    # --- Сообщаем клиенту его ID ---
    await send_to_one(writer, {"type": "ASSIGN_PLAYER_ID", "payload": {"player_id": player_entity_id}})

    # --- Отправка полного состояния новому клиенту ---
    initial_state = serialize_game_state_for_player(player_entity_id)
    await send_to_one(writer, {"type": "FULL_STATE_UPDATE", "payload": initial_state})
    logging.info(f"Sent full state snapshot to client {addr}")

    try:
        while True:
            data = await reader.readline()
            if not data:
                # Соединение разорвано клиентом
                break

            message_str = data.decode().strip()
            logging.debug(f"Received from {addr}: {message_str}")

            try:
                command = json.loads(message_str)
                if isinstance(command, dict):
                    command_type = command.get("type")

                    # --- Централизованная проверка хода ---
                    # Проверяем действия, которые можно выполнять только в свой ход.
                    # Это быстрая проверка, чтобы немедленно отклонить неверные команды.
                    # Основная проверка все равно остается в системах.
                    if command_type in ["PLAY_CARD", "DECLARE_ATTACKERS", "END_TURN", "TAP_LAND"]:
                        # Находим, чей сейчас ход, прямо из мира esper
                        active_player_ent = next((ent for ent, _ in esper.get_component(ActiveTurn)), -1)

                        # Если команду отправил неактивный игрок, отправляем ошибку и игнорируем команду.
                        if player_entity_id != active_player_ent:
                            logging.warning(f"[SECURITY] Player {player_entity_id} tried action '{command_type}' during Player {active_player_ent}'s turn. Denied.")
                            await send_to_one(writer, {"type": "ACTION_ERROR", "payload": {"message": "Сейчас не ваш ход."}})
                            continue # Переходим к следующему сообщению от клиента

                    if command_type == "PLAY_CARD":
                        payload = command.get("payload", {})
                        card_id = payload.get("card_entity_id")
                        target_id = payload.get("target_id") # Get optional target
                        if card_id is not None:
                            esper.create_entity(PlayCardCommand(
                                player_entity_id=player_entity_id,
                                card_entity_id=card_id,
                                target_id=target_id # Pass it to the command
                            ))
                        else:
                            logging.error(f"Command PLAY_CARD from {addr} is missing 'card_entity_id'.")
                    elif command_type == "END_TURN":
                        esper.create_entity(EndTurnCommand(
                            player_entity_id=player_entity_id
                        ))
                    elif command_type == "TAP_LAND":
                        payload = command.get("payload", {})
                        card_id = payload.get("card_entity_id")
                        if card_id is not None:
                            esper.create_entity(TapLandCommand(
                                player_entity_id=player_entity_id,
                                card_entity_id=card_id
                            ))
                    elif command_type == "DECLARE_BLOCKERS":
                        payload = command.get("payload", {})
                        blocks = payload.get("blocks", {})
                        # Преобразуем ключи из строк обратно в int
                        int_blocks = {int(k): v for k, v in blocks.items()}
                        esper.create_entity(DeclareBlockersCommand(
                            player_entity_id=player_entity_id, blocks=int_blocks))
                    elif command_type == "DECLARE_ATTACKERS":
                        payload = command.get("payload", {})
                        attacker_ids = payload.get("attacker_ids", [])
                        esper.create_entity(DeclareAttackersCommand(
                            player_entity_id=player_entity_id,
                            attacker_ids=attacker_ids
                        ))
                    # --- Новые команды для муллигана ---
                    elif command_type == "MULLIGAN":
                        esper.create_entity(MulliganCommand(player_entity_id=player_entity_id))
                    elif command_type == "KEEP_HAND":
                        esper.create_entity(KeepHandCommand(player_entity_id=player_entity_id))
                    elif command_type == "PUT_CARDS_BOTTOM":
                        payload = command.get("payload", {})
                        card_ids = payload.get("card_ids", [])
                        # Простая валидация на клиенте
                        if isinstance(card_ids, list):
                            esper.create_entity(PutCardsBottomCommand(
                                player_entity_id=player_entity_id,
                                card_ids=card_ids
                            ))
                    else: # Command is not a known game action
                        # Для других сообщений (или некорректных JSON-команд) работаем как чат
                        response = {"type": "chat", "from": str(addr), "payload": message_str}
                        await broadcast(response)
                else:
                    # Для других сообщений (или некорректных JSON-команд) работаем как чат
                    response = {"type": "chat", "from": str(addr), "payload": message_str}
                    await broadcast(response)
            except json.JSONDecodeError:
                # Если это вообще не JSON, тоже отправляем в чат
                logging.info(f"Received non-JSON data from {addr}, treating as chat.")
                response = {"type": "chat", "from": str(addr), "payload": message_str}
                await broadcast(response)

    except asyncio.CancelledError:
        pass
    finally:
        # Используем addr, полученный в начале, на случай если сокет уже закрыт
        logging.info(f"Client {addr} disconnected.")
        # Вместо удаления, помечаем игрока как отключенного
        disconnected_player_id = None
        for p_id, session in player_sessions.items():
            if session["writer"] == writer:
                session["status"] = "DISCONNECTED"
                session["writer"] = None
                disconnected_player_id = p_id
                break

        if disconnected_player_id:
            logging.info(f"Player {disconnected_player_id} marked as DISCONNECTED.")
            # Уведомляем оставшегося игрока
            await broadcast({"type": "PLAYER_DISCONNECTED", "payload": {"player_id": disconnected_player_id}})

        writer.close()
        try:
            # Закрытие сокета тоже может занять время, защищаемся таймаутом
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            logging.warning(f"Closing connection with {addr} timed out.")


# --- Игровой цикл ---
async def game_loop():
    """Основной игровой цикл, который работает независимо от сети."""
    tick_rate = 30  # 30 раз в секунду
    sleep_duration = 1.0 / tick_rate

    while True:
        try:
            loop_start_time = time.monotonic()

            # Системы обрабатывают логику и наполняют очередь событий
            esper.process()

            # Рассылаем все накопившиеся события клиентам
            if event_queue:
                logging.debug(f"Found {len(event_queue)} events to broadcast.")
                events_to_send = list(event_queue)
                event_queue.clear()

                # Определяем события, которые являются чисто информационными и не меняют состояние игры
                # таким образом, чтобы требовалась полная синхронизация.
                informational_event_types = {"ACTION_ERROR"}

                # Определяем события, которые сигнализируют о завершении основной фазы, после которой
                # мы хотим отложить обновление состояния, чтобы дать клиенту время на анимацию.
                phase_end_event_types = {"COMBAT_RESOLVED", "BLOCKERS_PHASE_STARTED"}

                # Определяем характер событий в очереди
                is_informational_only = all(
                    event.get("type") in informational_event_types for event in events_to_send
                )
                is_phase_end = any(
                    event.get("type") in phase_end_event_types for event in events_to_send
                )
                game_has_ended = any(event.get("type") == "GAME_OVER" for event in events_to_send)

                for event in events_to_send:
                    logging.debug(f"Broadcasting event: {event}")
                    await broadcast(event)

                # Решаем, отправлять ли полное обновление состояния. Мы отправляем его, только если
                # состояние действительно изменилось, и не в случаях, когда:
                # 1. Произошли только информационные события (например, ошибка действия).
                # 2. Завершилась фаза, требующая анимации на клиенте (например, бой).
                # 3. Игра закончилась.
                if not is_informational_only and not is_phase_end and not game_has_ended:
                    logging.debug("State changed, broadcasting tailored full state updates.")
                    for player_id, session in player_sessions.items():
                        if session["status"] == "CONNECTED" and session["writer"] is not None:
                            tailored_state = serialize_game_state_for_player(player_id)
                            await send_to_one(
                                session["writer"],
                                {"type": "FULL_STATE_UPDATE", "payload": tailored_state}
                            )
                else:
                    logging.debug(f"Skipping full state update (informational_only={is_informational_only}, is_phase_end={is_phase_end}, game_has_ended={game_has_ended}).")

            # Динамическая задержка для поддержания стабильного тикрейта
            loop_end_time = time.monotonic()
            processing_time = loop_end_time - loop_start_time
            sleep_time = max(0, sleep_duration - processing_time)
            await asyncio.sleep(sleep_time)

        except Exception as e:
            logging.exception("!!! UNHANDLED EXCEPTION IN GAME LOOP !!!")
            # Прерываем цикл, чтобы не спамить ошибками в консоль.
            break

def setup_new_game():
    """Initializes or resets the game state for a new match."""
    logging.info("--- SETTING UP NEW GAME ---")
    # Clear any existing entities and components
    esper.clear_database()

    # --- Инициализация игрового мира (продолжение) ---
    # Теперь игроки создаются со здоровьем
    player1_entity = esper.create_entity(Player(player_id=1, health=30), Graveyard())
    player2_entity = esper.create_entity(Player(player_id=2, health=30), Graveyard())
    logging.info(f"Created Player 1: entity {player1_entity}")
    logging.info(f"Created Player 2: entity {player2_entity}")

    # Создаем и перемешиваем колоды для каждого игрока
    p1_deck_ids = create_deck_for_player(player1_entity)
    random.shuffle(p1_deck_ids)
    esper.add_component(player1_entity, Deck(card_ids=p1_deck_ids))
    logging.info(f"Created deck for Player 1 with {len(p1_deck_ids)} cards.")

    p2_deck_ids = create_deck_for_player(player2_entity)
    random.shuffle(p2_deck_ids)
    esper.add_component(player2_entity, Deck(card_ids=p2_deck_ids))
    logging.info(f"Created deck for Player 2 with {len(p2_deck_ids)} cards.")

    # --- Стартовая рука ---
    # По правилам MTG каждый игрок берет 7 карт.
    for p_ent in [player1_entity, player2_entity]:
        player_deck = esper.component_for_entity(p_ent, Deck)
        if not player_deck.card_ids:
            continue
        for _ in range(7):
            if player_deck.card_ids:
                card_to_draw_id = player_deck.card_ids.pop(0)
                esper.remove_component(card_to_draw_id, InDeck)
                esper.add_component(card_to_draw_id, InHand())
                # Мы не отправляем событие CARD_DRAWN, так как клиент получит
                # полное состояние игры при подключении. Это позволяет избежать
                # отправки множества отдельных событий при запуске.
    logging.info("Dealt starting hands (7 cards) to both players.")
    
    # --- NEW: Start Mulligan Phase ---
    esper.create_entity(GamePhaseComponent(phase="MULLIGAN"))
    for p_ent in [player1_entity, player2_entity]:
        esper.add_component(p_ent, MulliganDecisionPhase())
        esper.add_component(p_ent, MulliganCount(count=0))
    logging.info("--- Starting Mulligan Phase ---")

def create_deck_for_player(player_entity_id: int) -> list[int]:
    """Создает набор карт для игрока и возвращает их ID."""
    deck_card_ids = []
    # Добавим заклинание "Огненный шар"
    card_templates = [
        {"name": "Plains", "cost": 0, "count": 15, "card_type": "LAND"},
        {"name": "Goblin", "cost": 1, "attack": 1, "health": 1, "count": 8, "card_type": "MINION"},
        {"name": "Knight", "cost": 3, "attack": 3, "health": 3, "count": 4, "card_type": "MINION"},
        {"name": "Fireball", "cost": 4, "count": 3, "card_type": "SPELL", "effect": {"type": "DEAL_DAMAGE", "value": 6, "requires_target": True}}
    ]
    for template in card_templates:
        for _ in range(template['count']):
            # Use a list to gather components before creating the entity
            card_components = [
                CardInfo(name=template['name'], cost=template['cost'], card_type=template['card_type']),
                Owner(player_entity_id=player_entity_id),
                InDeck()
            ]
            if template['card_type'] == "LAND":
                pass  # У земель нет доп. компонентов по умолчанию
            elif template['card_type'] == "MINION":
                card_components[0].attack = template['attack']
                card_components[0].health = template['health']
                card_components[0].max_health = template['health']
            elif template['card_type'] == "SPELL":
                effect_data = template['effect']
                card_components.append(SpellEffect(
                    effect_type=effect_data['type'],
                    value=effect_data['value'],
                    requires_target=effect_data['requires_target']
                ))

            card_id = esper.create_entity(*card_components)
            deck_card_ids.append(card_id)
    return deck_card_ids

async def main():
    """Главная функция хоста."""
    # --- Настройка логирования ---
    logging.basicConfig(
        level=logging.DEBUG,  # Установите logging.INFO для "боевого" режима
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logging.info("Server starting up...")

    # --- Инициализация систем ---
    # Передаем очередь событий в системы, чтобы они могли ее пополнять
    play_card_system = PlayCardSystem(event_queue=event_queue)
    attack_system = AttackSystem(event_queue=event_queue)
    tap_land_system = TapLandSystem(event_queue=event_queue)
    turn_management_system = TurnManagementSystem(event_queue=event_queue)
    # Передаем функцию `setup_new_game` в качестве колбэка для сброса
    game_over_system = GameOverSystem(event_queue=event_queue, reset_callback=setup_new_game)
    # Порядок важен. GameOverSystem должна идти первой, чтобы остановить игру.
    esper.add_processor(game_over_system)
    esper.add_processor(play_card_system)
    esper.add_processor(tap_land_system)
    esper.add_processor(attack_system)
    esper.add_processor(turn_management_system)
    # Добавляем систему муллигана
    esper.add_processor(MulliganSystem(event_queue=event_queue))

    # Первоначальная настройка игры
    setup_new_game()

    # --- Запуск сервера и игрового цикла ---
    server = await asyncio.start_server(handle_client, '0.0.0.0', 8888)

    addr = server.sockets[0].getsockname()
    logging.info(f'Server listening on {addr}')

    # Запускаем игровой цикл как фоновую задачу
    game_task = asyncio.create_task(game_loop())

    async with server:
        await server.serve_forever()


# --- Helper functions for serialization ---

def _get_card_location(card_ent: int) -> str:
    """Определяет местоположение карты (рука, стол и т.д.)."""
    if esper.has_component(card_ent, InHand): return "HAND"
    if esper.has_component(card_ent, OnBoard): return "BOARD"
    if esper.has_component(card_ent, InDeck): return "DECK"
    if esper.has_component(card_ent, InGraveyard): return "GRAVEYARD"
    return "UNKNOWN"

def _serialize_visible_card(card_ent: int, card_info: CardInfo) -> dict:
    """Сериализует данные видимой карты."""
    card_data = {
        "name": card_info.name,
        "cost": card_info.cost,
        "type": card_info.card_type,
        "is_tapped": esper.has_component(card_ent, Tapped),
        "is_attacking": esper.has_component(card_ent, Attacking),
        "has_sickness": esper.has_component(card_ent, SummoningSickness),
    }
    if card_info.card_type == "MINION":
        card_data["attack"] = card_info.attack
        card_data["health"] = card_info.health
        card_data["max_health"] = card_info.max_health
        card_data["can_attack"] = not card_data["is_tapped"] and not card_data["has_sickness"]
    elif card_info.card_type == "SPELL" and esper.has_component(card_ent, SpellEffect):
        effect = esper.component_for_entity(card_ent, SpellEffect)
        card_data["effect"] = {
            "type": effect.effect_type,
            "value": effect.value,
            "requires_target": effect.requires_target
        }
        card_data["can_attack"] = False
    else:
        card_data["can_attack"] = False
    return card_data

def _serialize_card(card_ent: int, card_info: CardInfo, owner: Owner, viewing_player_id: int) -> dict:
    """Сериализует одну карту, скрывая ее, если она в руке оппонента."""
    location = _get_card_location(card_ent)
    is_in_opponent_hand = (location == "HAND" and owner.player_entity_id != viewing_player_id)

    card_data = {
        "owner_id": owner.player_entity_id,
        "location": location,
    }

    if is_in_opponent_hand:
        card_data["is_hidden"] = True
    else:
        # Объединяем базовые данные с данными видимой карты
        card_data.update(_serialize_visible_card(card_ent, card_info))
    
    return card_data

def _get_active_player_id() -> int | None:
    """Находит ID активного игрока."""
    for _, (player, _) in esper.get_components(Player, ActiveTurn):
        return player.player_id
    return None

def _serialize_players_and_distribute_cards(all_cards: dict) -> dict:
    """Инициализирует данные игроков и распределяет карты по рукам и столам."""
    players_data = {}
    # Get the game phase once, outside the loop for efficiency
    game_phase_tuple = next(iter(esper.get_component(GamePhaseComponent)), None)
    game_phase = game_phase_tuple[1].phase if game_phase_tuple else "UNKNOWN"

    for player_ent, player in esper.get_component(Player):
        player_id_str = str(player.player_id)
        players_data[player_id_str] = {
            "entity_id": player_ent,
            "health": player.health,
            "mana_pool": player.mana_pool,
            "hand": [card_id for card_id, data in all_cards.items() if data["owner_id"] == player.player_id and data["location"] == "HAND"],
            "board": [card_id for card_id, data in all_cards.items() if data["owner_id"] == player.player_id and data["location"] == "BOARD"],
            "deck_size": len(esper.component_for_entity(player_ent, Deck).card_ids) if esper.has_component(player_ent, Deck) else 0,
            "graveyard_size": len(esper.component_for_entity(player_ent, Graveyard).card_ids) if esper.has_component(player_ent, Graveyard) else 0
        }
        # NEW: Add mulligan state info
        if game_phase == "MULLIGAN":
            if esper.has_component(player_ent, KeptHand):
                players_data[player_id_str]["mulligan_state"] = "WAITING"
            elif esper.has_component(player_ent, MulliganDecisionPhase):
                players_data[player_id_str]["mulligan_state"] = "DECIDING"
            else: # Must be in PUT_BOTTOM state
                mulligan_counter = esper.component_for_entity(player_ent, MulliganCount)
                players_data[player_id_str]["mulligan_state"] = "PUT_BOTTOM"
                players_data[player_id_str]["mulligan_put_bottom_count"] = mulligan_counter.count
    return players_data

def serialize_game_state_for_player(viewing_player_id: int) -> dict[str, Any]:
    """Собирает все данные из ECS мира в структурированный словарь для отправки клиенту,
    скрывая информацию, которую игрок не должен видеть (например, карты в руке оппонента).
    Эта функция-оркестратор вызывает вспомогательные функции для каждого шага."""
    all_cards = {
        card_ent: _serialize_card(card_ent, card_info, owner, viewing_player_id)
        for card_ent, (card_info, owner) in esper.get_components(CardInfo, Owner)
    }

    players_data = _serialize_players_and_distribute_cards(all_cards)

    # --- Add overall game phase ---
    game_phase = "UNKNOWN"
    game_phase_tuple = next(iter(esper.get_component(GamePhaseComponent)), None)
    if game_phase_tuple:
        game_phase = game_phase_tuple[1].phase

    state = {
        "players": players_data,
        "cards": all_cards,
        "active_player_id": _get_active_player_id(),
        "game_phase": game_phase
    }
    return state

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logging.info("Server is shutting down due to KeyboardInterrupt.")