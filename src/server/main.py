"""
Сервер для многопользовательской карточной игры Cardnet.

--- РЕАЛИЗОВАНО ---
- Асинхронный TCP-сервер на asyncio.
- Поддержка двух игроков с переподключением.
- Обнаружение сервера в локальной сети (UDP broadcast).
- Лобби с системой готовности игроков и чатом.
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
- Улучшенная валидация действий игроков на сервере.
- Сохранение состояния игры между перезапусками сервера.
"""
import asyncio
import json
import logging
import random
import socket
from typing import Any
import time
import esper
from src.common.components import (
    CardInfo, Player, Owner, InHand, OnBoard, PlayCardCommand, GameOver, SpellEffect, ActiveTurn, Graveyard,
    InGraveyard, EndTurnCommand, Deck, InDeck, Tapped, TapLandCommand, DeclareAttackersCommand,
    Attacking, SummoningSickness, DeclareBlockersCommand, Disconnected, PlayerReadyCommand,
    MulliganCommand, KeepHandCommand, PutCardsBottomCommand, MulliganDecisionPhase, MulliganCount, GamePhaseComponent, #
    KeptHand
)
from src.server.systems import (PlayCardSystem, TurnManagementSystem, AttackSystem, GameOverSystem, TapLandSystem,
                                MulliganSystem)

# --- Глобальное состояние ---
# Глобальное состояние для сессий игроков. Позволяет обрабатывать переподключения.
player_sessions = {
    1: {"writer": None, "status": "DISCONNECTED", "addr": None, "ready": False},
    2: {"writer": None, "status": "DISCONNECTED", "addr": None, "ready": False}
}
server_name = f"Cardnet Server {random.randint(1000, 9999)}"
BROADCAST_PORT = 8889
TCP_PORT = 8888
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


def _find_player_slot():
    """Находит свободный или отключенный слот для игрока."""
    for p_id, session in player_sessions.items():
        if session["status"] == "DISCONNECTED":
            is_reconnect = session["addr"] is not None
            return p_id, is_reconnect
    return None, False


async def _handle_new_connection(writer: asyncio.StreamWriter, player_id: int, is_reconnect: bool):
    """Обрабатывает начальную настройку для нового или переподключившегося клиента."""
    addr = writer.get_extra_info('peername', '???')

    # Обновляем сессию для подключившегося игрока
    player_sessions[player_id]["writer"] = writer
    player_sessions[player_id]["status"] = "CONNECTED"
    player_sessions[player_id]["addr"] = addr
    player_sessions[player_id]["ready"] = False # Сбрасываем готовность при подключении

    # Убираем маркер отключения из ECS
    if esper.entity_exists(player_id) and esper.has_component(player_id, Disconnected):
        esper.remove_component(player_id, Disconnected)

    logging.info(f"Client {addr} assigned to player ID {player_id}. Status: CONNECTED/RECONNECTED")

    # Если это было переподключение, уведомляем всех
    if is_reconnect:
        await broadcast({"type": "PLAYER_RECONNECTED", "payload": {"player_id": player_id}})

    # --- Сообщаем клиенту его ID ---
    await send_to_one(writer, {"type": "ASSIGN_PLAYER_ID", "payload": {"player_id": player_id}})

    # Если игра еще не началась (в лобби), просто уведомляем всех об обновлении лобби.
    game_phase_comp = next(iter(esper.get_component(GamePhaseComponent)), None)
    if game_phase_comp and game_phase_comp[1].phase == "LOBBY":
        # Создаем сериализуемую версию сессий, исключая не-JSON объекты (StreamWriter)
        serializable_sessions = {
            p_id: {"status": s_data["status"], "ready": s_data.get("ready", False)} for p_id, s_data in player_sessions.items()
        }
        await broadcast({"type": "LOBBY_UPDATE", "payload": {"sessions": serializable_sessions}})
        # Проверяем, оба ли игрока подключены и готовы, чтобы начать игру
        if all(s["status"] == "CONNECTED" and s.get("ready", False) for s in player_sessions.values()):
            logging.info("Both players ready. Starting new match.")
            start_new_match()
            # После начала матча нужна полная синхронизация
            await broadcast_full_state()
    else: # Игра уже идет, отправляем полное состояние только этому клиенту
        initial_state = serialize_game_state_for_player(player_id)
        await send_to_one(writer, {"type": "FULL_STATE_UPDATE", "payload": initial_state})
        logging.info(f"Sent full state snapshot to reconnected client {addr}")


async def _process_client_message(message_str: str, player_entity_id: int, writer: asyncio.StreamWriter):
    """Парсит и обрабатывает одно сообщение от клиента, создавая команды в ECS."""
    addr = writer.get_extra_info('peername', '???')
    try:
        command = json.loads(message_str)
        if not isinstance(command, dict):
            raise ValueError("Command is not a dictionary.")
    except (json.JSONDecodeError, ValueError):
        logging.info(f"Received non-JSON or invalid command from {addr}, treating as chat.")
        response = {"type": "chat", "from": str(addr), "payload": message_str}
        await broadcast(response)
        return

    command_type = command.get("type")
    payload = command.get("payload", {})

    # --- Централизованная проверка хода ---
    if command_type in ["PLAY_CARD", "DECLARE_ATTACKERS", "END_TURN", "TAP_LAND"]:
        active_player_ent = next((ent for ent, _ in esper.get_component(ActiveTurn)), -1)
        if player_entity_id != active_player_ent:
            logging.warning(f"[SECURITY] Player {player_entity_id} tried action '{command_type}' during Player {active_player_ent}'s turn. Denied.")
            await send_to_one(writer, {"type": "ACTION_ERROR", "payload": {"message": "Сейчас не ваш ход."}})
            return

    game_phase_comp = next(iter(esper.get_component(GamePhaseComponent)), None)
    current_phase = game_phase_comp[1].phase if game_phase_comp else "UNKNOWN"

    # --- Диспетчеризация команд ---
    if command_type == "CHAT_MESSAGE":
        if current_phase == "LOBBY":
            text = payload.get("text", "")
            if text: # Не отправляем пустые сообщения
                await broadcast({
                    "type": "CHAT_MESSAGE",
                    "payload": {
                        "sender_id": player_entity_id,
                        "text": text[:200] # Ограничиваем длину сообщения
                    }
                })
    elif command_type == "PLAYER_READY":
        if current_phase == "LOBBY" and player_entity_id in player_sessions:
            if not player_sessions[player_entity_id]["ready"]:
                player_sessions[player_entity_id]["ready"] = True
                logging.info(f"Player {player_entity_id} is ready.")
                # Уведомляем всех об изменении статуса
                serializable_sessions = {
                    p_id: {"status": s_data["status"], "ready": s_data.get("ready", False)} for p_id, s_data in player_sessions.items()
                }
                await broadcast({"type": "LOBBY_UPDATE", "payload": {"sessions": serializable_sessions}})
                # Проверяем, можно ли начать игру
                if all(s["status"] == "CONNECTED" and s.get("ready", False) for s in player_sessions.values()):
                    logging.info("Both players ready. Starting new match.")
                    start_new_match()
                    await broadcast_full_state()
    elif command_type == "PLAY_CARD":
        card_id = payload.get("card_entity_id")
        target_id = payload.get("target_id")
        if card_id is not None:
            esper.create_entity(PlayCardCommand(player_entity_id=player_entity_id, card_entity_id=card_id, target_id=target_id))
        else:
            logging.error(f"Command PLAY_CARD from {addr} is missing 'card_entity_id'.")
    elif command_type == "END_TURN":
        esper.create_entity(EndTurnCommand(player_entity_id=player_entity_id))
    elif command_type == "TAP_LAND":
        card_id = payload.get("card_entity_id")
        if card_id is not None:
            esper.create_entity(TapLandCommand(player_entity_id=player_entity_id, card_entity_id=card_id))
    elif command_type == "DECLARE_BLOCKERS":
        blocks = {int(k): v for k, v in payload.get("blocks", {}).items()}
        esper.create_entity(DeclareBlockersCommand(player_entity_id=player_entity_id, blocks=blocks))
    elif command_type == "DECLARE_ATTACKERS":
        attacker_ids = payload.get("attacker_ids", [])
        esper.create_entity(DeclareAttackersCommand(player_entity_id=player_entity_id, attacker_ids=attacker_ids))
    elif command_type == "MULLIGAN":
        esper.create_entity(MulliganCommand(player_entity_id=player_entity_id))
    elif command_type == "KEEP_HAND":
        esper.create_entity(KeepHandCommand(player_entity_id=player_entity_id))
    elif command_type == "PUT_CARDS_BOTTOM":
        card_ids = payload.get("card_ids", [])
        if isinstance(card_ids, list):
            esper.create_entity(PutCardsBottomCommand(player_entity_id=player_entity_id, card_ids=card_ids))
    else:
        logging.warning(f"Received unknown command type '{command_type}' from {addr}, treating as chat.")
        response = {"type": "chat", "from": str(addr), "payload": message_str}
        await broadcast(response)


async def _handle_disconnection(writer: asyncio.StreamWriter):
    """Обрабатывает отключение клиента, обновляя его статус."""
    addr = writer.get_extra_info('peername', '???')
    disconnected_player_id = None
    for p_id, session in player_sessions.items():
        if session["writer"] == writer:
            session["status"] = "DISCONNECTED"
            session["writer"] = None
            session["ready"] = False # Сбрасываем готовность при отключении
            disconnected_player_id = p_id
            break

    if disconnected_player_id:
        logging.info(f"Player {disconnected_player_id} ({addr}) marked as DISCONNECTED.")
        # Добавляем компонент в ECS, чтобы игровые системы знали о статусе
        if esper.entity_exists(disconnected_player_id):
            esper.add_component(disconnected_player_id, Disconnected())
        await broadcast({"type": "PLAYER_DISCONNECTED", "payload": {"player_id": disconnected_player_id}})
    else:
        logging.warning(f"A client ({addr}) disconnected, but was not found in active sessions.")

    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except asyncio.TimeoutError:
        logging.warning(f"Closing connection with {addr} timed out.")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """
    Эта корутина выполняется для каждого нового подключения клиента.
    Она находит слот для игрока, обрабатывает его сообщения и отключение.
    """
    addr = writer.get_extra_info('peername', '???')
    logging.info(f"New client trying to connect: {addr}")

    player_entity_id, is_reconnect = _find_player_slot()

    if player_entity_id is None:
        logging.warning(f"Connection rejected for {addr}: all slots are full.")
        await send_to_one(writer, {"type": "GAME_FULL"})
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return

    await _handle_new_connection(writer, player_entity_id, is_reconnect)

    try:
        while True:
            data = await reader.readline()
            if not data: break
            message_str = data.decode().strip()
            logging.debug(f"Received from {addr}: {message_str}")
            await _process_client_message(message_str, player_entity_id, writer)
    except asyncio.CancelledError:
        logging.info(f"Client handler for {addr} cancelled.")
    finally:
        await _handle_disconnection(writer)


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
                    await broadcast_full_state()
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

async def broadcast_full_state():
    """Рассылает каждому игроку его версию состояния игры."""
    logging.debug("Broadcasting tailored full state updates.")
    for player_id, session in player_sessions.items():
        if session["status"] == "CONNECTED" and session["writer"] is not None:
            tailored_state = serialize_game_state_for_player(player_id)
            await send_to_one(
                session["writer"],
                {"type": "FULL_STATE_UPDATE", "payload": tailored_state}
            )

def start_new_match():
    """Initializes or resets the game state for a new match."""
    logging.info("--- STARTING NEW MATCH ---")
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

def reset_world():
    """Сбрасывает мир в состояние лобби, готовое к новой игре."""
    logging.info("--- Resetting world to LOBBY state ---")
    esper.clear_database()
    # Создаем единственный компонент, который говорит, что мы в лобби
    esper.create_entity(GamePhaseComponent(phase="LOBBY"))

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

async def udp_server_broadcast(tcp_port: int):
    """Периодически рассылает информацию о сервере по UDP."""
    # Создаем UDP сокет
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    # Включаем режим broadcast
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Устанавливаем таймаут, чтобы сокет не блокировал вечно
    sock.settimeout(0.2)
    # Адрес '<broadcast>' работает на всех платформах
    broadcast_address = ('<broadcast>', BROADCAST_PORT)
    
    logging.info(f"Starting UDP broadcast on port {BROADCAST_PORT}")

    while True:
        try:
            # Готовим сообщение
            connected_players = sum(1 for s in player_sessions.values() if s['status'] == 'CONNECTED')
            game_phase_comp = next(iter(esper.get_component(GamePhaseComponent)), None)
            game_phase = game_phase_comp[1].phase if game_phase_comp else "UNKNOWN"

            message = {
                "server_name": server_name,
                "players": f"{connected_players}/{len(player_sessions)}",
                "status": game_phase,
                "tcp_port": tcp_port
            }
            encoded_message = json.dumps(message).encode('utf-8')

            sock.sendto(encoded_message, broadcast_address)
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Error in UDP broadcast loop: {e}")
            await asyncio.sleep(10)

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
    # Передаем функцию `reset_world` в качестве колбэка для сброса
    game_over_system = GameOverSystem(event_queue=event_queue, reset_callback=reset_world)
    # Порядок важен. GameOverSystem должна идти первой, чтобы остановить игру.
    esper.add_processor(game_over_system)
    esper.add_processor(play_card_system)
    esper.add_processor(tap_land_system)
    esper.add_processor(attack_system)
    esper.add_processor(turn_management_system)
    # Добавляем систему муллигана
    esper.add_processor(MulliganSystem(event_queue=event_queue))

    # Первоначальная настройка мира в состояние лобби
    reset_world()

    # --- Запуск сервера и игрового цикла ---
    server = await asyncio.start_server(handle_client, '0.0.0.0', TCP_PORT)

    addr = server.sockets[0].getsockname()
    logging.info(f'Server listening on {addr}')

    # Запускаем игровой цикл как фоновую задачу
    game_task = asyncio.create_task(game_loop())
    # Запускаем UDP broadcast как фоновую задачу
    broadcast_task = asyncio.create_task(udp_server_broadcast(TCP_PORT))

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
            "graveyard_size": len(esper.component_for_entity(player_ent, Graveyard).card_ids) if esper.has_component(player_ent, Graveyard) else 0,
            "is_connected": not esper.has_component(player_ent, Disconnected)
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