import unittest
import esper
from unittest.mock import Mock
import pygame
import queue

# Импортируем систему и компоненты, которые тестируем
from src.client.main import StateUpdateSystem, ClientState, Drawable, Position, Clickable, GamePhase


class TestClientSynchronization(unittest.TestCase):
    """Тестирует логику синхронизации мира клиента с состоянием сервера."""

    def setUp(self):
        """Настраивает окружение перед каждым тестом."""
        esper.clear_database()
        pygame.init()
        # "Мокаем" (имитируем) зависимости, которые не важны для теста
        self.mock_font = Mock()
        # self.font.render() должен возвращать Surface, а не другой Mock.
        # Создаем небольшую "пустышку" для этого.
        self.mock_font.render.return_value = pygame.Surface((10, 10))
        self.client_state = ClientState()
        self.incoming_queue = queue.Queue()
        self.discovery_queue = queue.Queue()
        self.state_update_system = StateUpdateSystem(
            incoming_q=self.incoming_queue,
            discovery_q=self.discovery_queue,
            font=self.mock_font,
            client_state=self.client_state
        )
        # Устанавливаем ID нашего игрока для тестов
        self.client_state.my_player_id = 1

    def tearDown(self):
        """Очищает мир после каждого теста."""
        esper.clear_database()

    def test_synchronize_world_creates_entities_and_components(self):
        """Проверяет, что _synchronize_world создает сущности и нужные компоненты."""
        """Проверяет, что _synchronize_world создает сущности и нужные компоненты,
        включая корректную обработку карт в руке оппонента."""
        # Подготовка: создаем тестовый словарь состояния, как будто он пришел от сервера
        server_state = {
            "players": {
                "1": {"entity_id": 1, "health": 30, "hand": [3], "board": []},
                "2": {"entity_id": 2, "health": 30, "hand": [4], "board": [5]}
            },
            "cards": {
                "3": {"name": "My Goblin", "owner_id": 1, "location": "HAND", "type": "MINION"},
                "4": {"name": "Opponent's Imp", "owner_id": 2, "location": "HAND", "type": "MINION"},
                "5": {"name": "Opponent's Knight", "owner_id": 2, "location": "BOARD", "type": "MINION"}
            },
            "active_player_id": 1
        }

        # Действие: вызываем тестируемый метод
        self.state_update_system._synchronize_world(server_state)

        # Проверки:
        # 1. Сущности для игроков и карт должны быть созданы
        self.assertTrue(esper.entity_exists(1), "Сущность игрока 1 должна существовать")
        self.assertTrue(esper.entity_exists(2), "Сущность игрока 2 должна существовать")
        self.assertTrue(esper.entity_exists(3), "Сущность карты 3 (моя рука) должна существовать")
        self.assertTrue(esper.entity_exists(4), "Сущность карты 4 (рука оппонента) должна существовать")
        self.assertTrue(esper.entity_exists(5), "Сущность карты 5 (стол оппонента) должна существовать")

        # 2. Карты должны получить "рисуемые" компоненты
        self.assertTrue(esper.has_component(3, Drawable))
        self.assertTrue(esper.has_component(3, Position))
        self.assertTrue(esper.has_component(4, Drawable))
        self.assertTrue(esper.has_component(4, Position))
        self.assertTrue(esper.has_component(5, Drawable))
        self.assertTrue(esper.has_component(5, Position))

        # 3. Только мои карты в руке и карты на столе должны быть кликабельными
        self.assertTrue(esper.has_component(3, Clickable), "Моя карта в руке должна быть кликабельной")
        self.assertTrue(esper.has_component(5, Clickable), "Карта на столе должна быть кликабельной")
        self.assertFalse(esper.has_component(4, Clickable), "Карта в руке оппонента не должна быть кликабельной")

        # 4. Сущности игроков не должны быть "рисуемыми" (по текущей логике)
        self.assertFalse(esper.has_component(1, Drawable))
        self.assertFalse(esper.has_component(2, Drawable))

    def test_synchronize_world_handles_gaps_in_ids(self):
        """Проверяет, что синхронизация работает, даже если ID идут не по порядку (карта умерла)."""
        # Состояние, где карта с ID=4 отсутствует
        server_state = {
            "players": {"1": {"entity_id": 1, "hand": [], "board": []}, "2": {"entity_id": 2, "hand": [], "board": []}},
            "cards": {
                "3": {"name": "A", "owner_id": 1, "location": "BOARD"},
                "5": {"name": "B", "owner_id": 2, "location": "BOARD"}
            },
        }
        self.state_update_system._synchronize_world(server_state)

        # Проверка: сущность 4 должна быть создана как "пустышка", чтобы не нарушать ID
        self.assertTrue(esper.entity_exists(4), "Промежуточная сущность 4 должна быть создана")
        self.assertFalse(esper.has_component(4, Drawable), "Промежуточная сущность не должна быть видимой")

    def test_full_state_update_event_preserves_phase(self):
        """Проверяет, что FULL_STATE_UPDATE сбрасывает выбор, но СОХРАНЯЕТ фазу."""
        # Подготовка: устанавливаем состояние выбора и нестандартную фазу
        self.client_state.selected_entity = 10
        self.client_state.selected_blocker = 20
        self.client_state.phase = GamePhase.MAIN_2 # Нестандартная фаза
        
        # Помещаем событие в очередь
        event = {"type": "FULL_STATE_UPDATE", "payload": {"players": {}, "cards": {}}}
        self.incoming_queue.put(event)

        # Действие: обрабатываем очередь
        self.state_update_system.process()

        # Проверка: состояние выбора должно быть сброшено
        self.assertIsNone(self.client_state.selected_entity)
        self.assertIsNone(self.client_state.selected_blocker)
        self.assertEqual(self.client_state.phase, GamePhase.MAIN_2, "Фаза не должна сбрасываться при полном обновлении состояния")

    def test_action_error_event_adds_to_log(self):
        """Проверяет, что событие ACTION_ERROR добавляет сообщение в лог."""
        # Подготовка: помещаем событие в очередь
        error_message = "Недостаточно маны"
        event = {"type": "ACTION_ERROR", "payload": {"message": error_message}}
        self.incoming_queue.put(event)

        # Действие: обрабатываем очередь
        self.state_update_system.process()

        # Проверка: сообщение должно появиться в логе
        self.assertEqual(len(self.client_state.log_messages), 1)
        self.assertIn(error_message, self.client_state.log_messages[0])

    def test_log_is_trimmed_to_max_size(self):
        """Проверяет, что лог обрезается до максимального размера."""
        # Подготовка: заполняем лог большим количеством сообщений
        self.client_state.max_log_messages = 5
        for i in range(10):
            self.state_update_system._add_log_message(f"Сообщение {i}")

        # Проверка: размер лога не должен превышать максимальный
        self.assertEqual(len(self.client_state.log_messages), 5)
        # Проверяем, что в логе остались последние сообщения
        self.assertEqual(self.client_state.log_messages[0], "Сообщение 5")
        self.assertEqual(self.client_state.log_messages[-1], "Сообщение 9")

    def test_game_over_event_sets_state(self):
        """Проверяет, что событие GAME_OVER корректно устанавливает состояние конца игры."""
        self.client_state.game_over = False
        self.client_state.winner_id = None

        event = {"type": "GAME_OVER", "payload": {"winner_id": self.client_state.my_player_id}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        self.assertTrue(self.client_state.game_over)
        self.assertEqual(self.client_state.winner_id, self.client_state.my_player_id)

    def test_blockers_phase_started_event_sets_phase_and_attackers(self):
        """Проверяет, что событие BLOCKERS_PHASE_STARTED устанавливает фазу и список атакующих."""
        self.client_state.phase = GamePhase.MAIN_1
        self.client_state.attackers = []
        # Добавляем карты в состояние, чтобы их можно было изменить
        self.client_state.game_state_dict = {
            "cards": {
                "10": {"name": "Attacker 1", "is_attacking": False},
                "11": {"name": "Attacker 2", "is_attacking": False},
            }
        }

        attackers_list = [10, 11]
        event = {"type": "BLOCKERS_PHASE_STARTED", "payload": {"attackers": attackers_list}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        self.assertEqual(self.client_state.phase, GamePhase.COMBAT_DECLARE_BLOCKERS)
        self.assertEqual(self.client_state.attackers, attackers_list)
        self.assertIsNone(self.client_state.selected_blocker)
        self.assertEqual(self.client_state.block_assignments, {})
        # Проверяем, что флаг атаки установлен
        self.assertTrue(self.client_state.game_state_dict["cards"]["10"]["is_attacking"])
        self.assertTrue(self.client_state.game_state_dict["cards"]["11"]["is_attacking"])

    def test_combat_resolved_event_sets_phase_to_main_2(self):
        """Проверяет, что событие COMBAT_RESOLVED устанавливает фазу MAIN_2 и сбрасывает состояние боя."""
        # Подготовка: имитируем активную фазу блокирования
        self.client_state.phase = GamePhase.COMBAT_DECLARE_BLOCKERS
        self.client_state.attackers = [10, 11]
        self.client_state.selected_blocker = 20
        self.client_state.block_assignments = {20: 10}
        # Добавляем карты в состояние, чтобы их можно было изменить
        self.client_state.game_state_dict = {
            "cards": {
                "10": {"name": "Attacker 1", "is_attacking": True},
                "11": {"name": "Attacker 2", "is_attacking": True},
            }
        }

        event = {"type": "COMBAT_RESOLVED", "payload": {}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        self.assertEqual(self.client_state.phase, GamePhase.MAIN_2)
        self.assertEqual(self.client_state.attackers, [])
        self.assertIsNone(self.client_state.selected_blocker)
        self.assertEqual(self.client_state.block_assignments, {})
        # Проверяем, что флаг атаки сброшен
        self.assertFalse(self.client_state.game_state_dict["cards"]["10"]["is_attacking"])
        self.assertFalse(self.client_state.game_state_dict["cards"]["11"]["is_attacking"])

    def test_turn_started_event_resets_phase_to_main_1(self):
        """Проверяет, что событие TURN_STARTED сбрасывает фазу в MAIN_1."""
        self.client_state.phase = GamePhase.MAIN_2
        self.client_state.pending_attackers = [123] # some dummy data

        event = {"type": "TURN_STARTED", "payload": {"player_id": 1}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        self.assertEqual(self.client_state.phase, GamePhase.MAIN_1)
        self.assertEqual(self.client_state.pending_attackers, [])

    def test_player_damaged_event_updates_health_and_queues_animation(self):
        """Проверяет, что PLAYER_DAMAGED обновляет здоровье и добавляет анимацию."""
        # Подготовка: устанавливаем начальное состояние
        player_entity_id = 2
        self.client_state.game_state_dict = {
            "players": {
                str(player_entity_id): {"entity_id": player_entity_id, "health": 30}
            }
        }

        event = {"type": "PLAYER_DAMAGED", "payload": {"player_id": player_entity_id, "new_health": 25}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        # Проверка: здоровье должно обновиться в локальном состоянии
        updated_health = self.client_state.game_state_dict["players"][str(player_entity_id)]["health"]
        self.assertEqual(updated_health, 25)

        # Проверка: событие должно быть добавлено в очередь анимаций
        self.assertEqual(len(self.client_state.animation_queue), 1)
        self.assertEqual(self.client_state.animation_queue[0], event)

    def test_card_died_event_queues_animation_and_logs(self):
        """Проверяет, что CARD_DIED добавляет анимацию в очередь и сообщение в лог."""
        card_id_to_die = 15
        self.client_state.game_state_dict = {"cards": {str(card_id_to_die): {"name": "Goblin"}}}

        event = {"type": "CARD_DIED", "payload": {"card_id": card_id_to_die}}
        self.incoming_queue.put(event)
        self.state_update_system.process()

        self.assertEqual(len(self.client_state.animation_queue), 1)
        self.assertEqual(self.client_state.animation_queue[0], event)
        self.assertIn("'Goblin' уничтожена.", self.client_state.log_messages[0])