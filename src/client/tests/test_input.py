import unittest
from unittest.mock import Mock, patch
import queue
import pygame
import esper

from src.client.main import InputSystem, ClientState, Position, Drawable, Clickable, CardSprite, UIManager, RenderSystem, GamePhase

class TestInputSystem(unittest.TestCase):
    def setUp(self):
        """Настраивает тестовое окружение перед каждым тестом."""
        esper.clear_database()
        pygame.init()
        self.client_state = ClientState()
        self.outgoing_queue = queue.Queue()
        self.ui_manager = UIManager()
        
        # Мокаем RenderSystem, так как InputSystem зависит от него для получения координат портретов
        self.mock_render_system = Mock(spec=RenderSystem)
        self.mock_render_system._get_player_portrait_rect.return_value = pygame.Rect(100, 100, 100, 140)
        
        # Заставляем esper.get_processor возвращать наш мок
        self.patcher = patch('esper.get_processor', return_value=self.mock_render_system)
        self.mock_get_processor = self.patcher.start()

        self.input_system = InputSystem(
            outgoing_q=self.outgoing_queue,
            client_state=self.client_state,
            ui_manager=self.ui_manager
        )
        
        # Общая настройка состояния
        self.client_state.my_player_id = 1
        self.client_state.active_player_id = 1
        self.client_state.phase = GamePhase.MAIN_1
        self.client_state.game_state_dict = {
            "players": {
                "1": {"entity_id": 1, "hand": [], "board": []},
                "2": {"entity_id": 2, "hand": [], "board": []}
            },
            "cards": {}
        }
        self.mock_font = Mock()
        self.mock_font.render.return_value = pygame.Surface((10, 10))

    def tearDown(self):
        """Очищает окружение после каждого теста."""
        self.patcher.stop()
        esper.clear_database()

    def _create_card(self, entity_id, owner_id, location, card_type, pos=(10, 10), **card_props):
        """Вспомогательная функция для создания карты со всеми необходимыми компонентами."""
        card_data = {
            "owner_id": owner_id,
            "location": location,
            "type": card_type,
            **card_props
        }
        # Гарантируем, что сущность с нужным ID существует
        while not esper.entity_exists(entity_id):
            esper.create_entity()
        
        card_sprite = CardSprite(entity_id, card_data, self.mock_font)
        # В тестах мы должны вручную установить позицию rect,
        # так как RenderSystem не запускается для этого.
        card_sprite.rect.topleft = pos
        esper.add_component(entity_id, Drawable(card_sprite))
        esper.add_component(entity_id, Position(x=pos[0], y=pos[1]))
        esper.add_component(entity_id, Clickable())
        
        # Добавляем данные в "представление" мира в client_state
        self.client_state.game_state_dict["cards"][str(entity_id)] = card_data
        if location == "HAND":
            self.client_state.game_state_dict["players"][str(owner_id)]["hand"].append(entity_id)
        elif location == "BOARD":
            self.client_state.game_state_dict["players"][str(owner_id)]["board"].append(entity_id)
        
        return entity_id

    @patch('pygame.event.get')
    def test_right_click_cancels_selection(self, mock_event_get):
        """Проверяет, что правый клик отменяет выбор сущности."""
        self.client_state.selected_entity = 101
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=3, pos=(0, 0))
        ]
        
        self.input_system.process()
        
        self.assertIsNone(self.client_state.selected_entity)

    @patch('pygame.event.get')
    def test_click_card_in_hand_sends_play_command(self, mock_event_get):
        """Проверяет, что клик по карте существа в руке в главной фазе отправляет команду PLAY_CARD."""
        card_id = self._create_card(10, 1, "HAND", "MINION", pos=(200, 500))
        self.client_state.phase = GamePhase.MAIN_1
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(200, 500))
        ]
        
        self.input_system.process()
        
        self.assertFalse(self.outgoing_queue.empty(), "Очередь исходящих команд не должна быть пустой")
        command = self.outgoing_queue.get()
        self.assertEqual(command["type"], "PLAY_CARD")
        self.assertEqual(command["payload"]["card_entity_id"], card_id)

    @patch('pygame.event.get')
    def test_click_tappable_land_on_board_sends_tap_command(self, mock_event_get):
        """Проверяет, что клик по земле на столе в главной фазе отправляет команду TAP_LAND."""
        card_id = self._create_card(11, 1, "BOARD", "LAND", pos=(300, 300), is_tapped=False)
        self.client_state.phase = GamePhase.MAIN_1
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(300, 300))
        ]
        
        self.input_system.process()
        
        self.assertFalse(self.outgoing_queue.empty())
        command = self.outgoing_queue.get()
        self.assertEqual(command["type"], "TAP_LAND")
        self.assertEqual(command["payload"]["card_entity_id"], card_id)

    @patch('pygame.event.get')
    def test_declare_attackers_phase_selects_and_deselects_minion(self, mock_event_get):
        """Проверяет, что в фазе атаки клик по существу добавляет/убирает его из кандидатов в атакующие."""
        self.client_state.phase = GamePhase.COMBAT_DECLARE_ATTACKERS
        card_id = self._create_card(12, 1, "BOARD", "MINION", pos=(400, 300), can_attack=True)

        # Первый клик - выбрать
        mock_event_get.return_value = [pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(400, 300))]
        self.input_system.process()

        self.assertTrue(self.outgoing_queue.empty(), "Не должно отправляться команд при выборе атакующего")
        self.assertIn(card_id, self.client_state.pending_attackers, "Существо должно быть добавлено в список атакующих")

        # Второй клик - отменить выбор
        mock_event_get.return_value = [pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(400, 300))]
        self.input_system.process()
        self.assertNotIn(card_id, self.client_state.pending_attackers, "Существо должно быть убрано из списка атакующих")

    def test_declare_attackers_sends_command_and_changes_phase(self):
        """Проверяет, что вызов declare_attackers отправляет команду и меняет фазу."""
        self.client_state.phase = GamePhase.COMBAT_DECLARE_ATTACKERS
        attacker_id = 12
        self.client_state.pending_attackers = [attacker_id]

        self.input_system.declare_attackers()

        self.assertFalse(self.outgoing_queue.empty())
        command = self.outgoing_queue.get()
        self.assertEqual(command["type"], "DECLARE_ATTACKERS")
        self.assertEqual(command["payload"]["attacker_ids"], [attacker_id])
        self.assertEqual(self.client_state.phase, GamePhase.COMBAT_AWAITING_CONFIRMATION, "Фаза должна смениться на ожидание подтверждения от сервера")

    def test_declare_attackers_with_no_attackers_changes_phase_locally(self):
        """Проверяет, что объявление атаки без атакующих сразу меняет фазу на MAIN_2 без отправки команды."""
        self.client_state.phase = GamePhase.COMBAT_DECLARE_ATTACKERS
        self.client_state.pending_attackers = []

        self.input_system.declare_attackers()

        self.assertTrue(self.outgoing_queue.empty(), "Не должно отправляться команд, если нет атакующих")
        self.assertEqual(self.client_state.phase, GamePhase.MAIN_2, "Фаза должна смениться на MAIN_2")


    @patch('pygame.event.get')
    def test_click_spell_with_target_selects_card(self, mock_event_get):
        """Проверяет, что клик по заклинанию с целью в главной фазе выбирает его."""
        self.client_state.phase = GamePhase.MAIN_1
        card_id = self._create_card(
            13, 1, "HAND", "SPELL", pos=(500, 500), 
            effect={"requires_target": True}
        )
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(500, 500))
        ]
        
        self.input_system.process()
        
        self.assertTrue(self.outgoing_queue.empty(), "Очередь должна быть пустой, карта должна быть выбрана")
        self.assertEqual(self.client_state.selected_entity, card_id)

    @patch('pygame.event.get')
    def test_click_target_after_spell_selection_sends_command(self, mock_event_get):
        """Проверяет, что клик по цели после выбора заклинания в главной фазе отправляет команду."""
        self.client_state.phase = GamePhase.MAIN_1
        spell_id = self._create_card(
            13, 1, "HAND", "SPELL", pos=(500, 500), 
            effect={"requires_target": True}
        )
        self.client_state.selected_entity = spell_id
        
        # Портрет оппонента - наша цель
        opponent_portrait_pos = (150, 150)
        self.mock_render_system._get_player_portrait_rect.return_value = pygame.Rect(opponent_portrait_pos[0], opponent_portrait_pos[1], 100, 140)
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=opponent_portrait_pos)
        ]
        
        self.input_system.process()
        
        self.assertFalse(self.outgoing_queue.empty())
        command = self.outgoing_queue.get()
        self.assertEqual(command["type"], "PLAY_CARD")
        self.assertEqual(command["payload"]["card_entity_id"], spell_id)
        self.assertEqual(command["payload"]["target_id"], 2) # ID сущности оппонента
        self.assertIsNone(self.client_state.selected_entity, "Выбор должен сброситься после указания цели")

    @patch('pygame.event.get')
    def test_blocking_phase_select_blocker(self, mock_event_get):
        """Проверяет выбор валидного блокера в фазе блокирования."""
        # Ход оппонента, мы защищаемся
        self.client_state.phase = GamePhase.COMBAT_DECLARE_BLOCKERS
        self.client_state.active_player_id = 2
        
        blocker_id = self._create_card(20, 1, "BOARD", "MINION", pos=(300, 300), is_tapped=False)
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(300, 300))
        ]
        
        self.input_system.process()
        
        self.assertTrue(self.outgoing_queue.empty())
        self.assertEqual(self.client_state.selected_blocker, blocker_id)

    @patch('pygame.event.get')
    def test_blocking_phase_assign_blocker_to_attacker(self, mock_event_get):
        """Проверяет назначение выбранного блокера на атакующего."""
        self.client_state.phase = GamePhase.COMBAT_DECLARE_BLOCKERS
        self.client_state.active_player_id = 2
        
        blocker_id = self._create_card(20, 1, "BOARD", "MINION", pos=(300, 300), is_tapped=False)
        attacker_id = self._create_card(21, 2, "BOARD", "MINION", pos=(400, 200), is_attacking=True)
        
        self.client_state.selected_blocker = blocker_id
        self.client_state.attackers = [attacker_id]
        
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(400, 200))
        ]
        
        self.input_system.process()
        
        self.assertTrue(self.outgoing_queue.empty())
        self.assertIn(blocker_id, self.client_state.block_assignments)
        self.assertEqual(self.client_state.block_assignments[blocker_id], attacker_id)
        self.assertIsNone(self.client_state.selected_blocker, "Блокер должен быть снят с выбора после назначения")
