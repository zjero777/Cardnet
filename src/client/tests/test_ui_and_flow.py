import unittest
from unittest.mock import Mock, patch
import queue
import pygame
import esper

from src.client.main import (
    ClientState, UIManager, UISetupSystem, InputSystem, StateUpdateSystem,
    Button, TextInput, GamePhase
)
from src.client.ui import MENU_BUTTON_TEXT

class TestUIAndFlow(unittest.TestCase):
    """Тестирует UI и логику переключения между экранами (меню, список серверов, лобби)."""

    def setUp(self):
        """Настраивает окружение перед каждым тестом."""
        pygame.init()
        esper.clear_database()

        self.client_state = ClientState()
        self.ui_manager = UIManager()
        self.outgoing_queue = queue.Queue()
        self.discovery_queue = queue.Queue()

        # Моки для колбэков, которые управляют сетевыми потоками
        self.mock_start_connection = Mock()
        self.mock_reset_to_menu = Mock()
        self.mock_disconnect = Mock()

        # Создаем основные системы
        self.mock_font = Mock()
        self.mock_font.render.return_value = pygame.Surface((10, 10))
        
        self.chat_input = TextInput(
            rect=pygame.Rect(0, 0, 100, 30),
            font=self.mock_font,
            text_color=MENU_BUTTON_TEXT
        )

        self.ui_setup_system = UISetupSystem(
            self.client_state, self.ui_manager, self.mock_font, self.mock_font,
            self.mock_start_connection, self.mock_reset_to_menu, self.mock_disconnect, self.chat_input
        )
        self.input_system = InputSystem(
            self.outgoing_queue, self.client_state, self.ui_manager, self.chat_input
        )
        self.state_update_system = StateUpdateSystem(
            queue.Queue(), self.discovery_queue, self.mock_font, self.client_state
        )

        esper.add_processor(self.ui_setup_system)
        esper.add_processor(self.input_system)
        esper.add_processor(self.state_update_system)

    def tearDown(self):
        """Очищает мир после каждого теста."""
        esper.clear_database()
        esper._processors.clear()

    def test_initial_state_is_main_menu(self):
        """Проверяет, что начальное состояние - это главное меню с двумя кнопками."""
        self.assertEqual(self.client_state.game_phase, "MAIN_MENU")
        
        esper.process() # Запускаем UISetupSystem

        buttons = [el for el in self.ui_manager.elements if isinstance(el, Button)]
        self.assertEqual(len(buttons), 2)
        button_texts = {b.text for b in buttons}
        self.assertIn("Присоединиться к игре", button_texts)
        self.assertIn("Выход", button_texts)

    def test_join_game_button_switches_to_server_browser(self):
        """Проверяет, что нажатие кнопки 'Присоединиться' переключает фазу на поиск серверов."""
        esper.process() # Создаем кнопки главного меню
        
        join_button = next(b for b in self.ui_manager.elements if b.text == "Присоединиться к игре")
        join_button.callback() # Имитируем нажатие

        self.assertEqual(self.client_state.game_phase, "SERVER_BROWSER")

    def test_server_discovery_event_populates_list(self):
        """Проверяет, что событие SERVER_FOUND добавляет сервер в список."""
        server_info = {"server_name": "Test Server", "ip": "127.0.0.1", "tcp_port": 8888}
        self.discovery_queue.put({"type": "SERVER_FOUND", "payload": server_info})

        esper.process() # Запускаем StateUpdateSystem

        self.assertIn(("127.0.0.1", 8888), self.client_state.server_list)
        self.assertEqual(self.client_state.server_list[("127.0.0.1", 8888)]["server_name"], "Test Server")

    def test_server_browser_creates_buttons_for_servers(self):
        """Проверяет, что для каждого найденного сервера создается кнопка."""
        self.client_state.game_phase = "SERVER_BROWSER"
        self.client_state.server_list[("192.168.1.10", 8888)] = {
            "server_name": "My Game", "players": "1/2", "status": "LOBBY", "ip": "192.168.1.10", "tcp_port": 8888
        }

        esper.process() # Запускаем UISetupSystem

        server_button = next((b for b in self.ui_manager.elements if isinstance(b, Button) and "My Game" in b.text), None)
        self.assertIsNotNone(server_button)

        # Проверяем, что клик по кнопке вызывает колбэк подключения
        server_button.callback()
        self.mock_start_connection.assert_called_once_with("192.168.1.10", 8888)

    def test_lobby_shows_ready_and_back_buttons(self):
        """Проверяет, что в лобби отображаются кнопки 'Готов' и 'Назад'."""
        self.client_state.game_phase = "LOBBY"
        self.client_state.my_player_id = 1
        self.client_state.lobby_state = {"1": {"ready": False}}

        esper.process() # Запускаем UISetupSystem

        button_texts = {b.text for b in self.ui_manager.elements if isinstance(b, Button)}
        self.assertIn("Готов", button_texts)
        self.assertIn("Назад", button_texts)

    @patch('pygame.event.get')
    def test_ready_button_sends_command(self, mock_event_get):
        """Проверяет, что нажатие на кнопку 'Готов' отправляет команду на сервер."""
        self.client_state.game_phase = "LOBBY"
        self.client_state.my_player_id = 1
        self.client_state.lobby_state = {"1": {"ready": False}}
        esper.process() # Создаем UI

        ready_button = next(b for b in self.ui_manager.elements if b.text == "Готов")
        
        # Имитируем клик через InputSystem, который является частью esper.process()
        mock_event_get.return_value = [
            pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=ready_button.rect.center),
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=ready_button.rect.center)
        ]
        esper.process() # Запускаем все системы, включая InputSystem

        self.assertFalse(self.outgoing_queue.empty())
        command = self.outgoing_queue.get_nowait()
        self.assertEqual(command["type"], "PLAYER_READY")

    @patch('pygame.event.get')
    def test_chat_input_sends_message_on_enter(self, mock_event_get):
        """Проверяет, что ввод текста в чат и нажатие Enter отправляет команду."""
        self.client_state.game_phase = "LOBBY"
        self.chat_input.is_active = True
        self.chat_input.text = "Hello World"

        # Имитируем нажатие Enter
        mock_event_get.return_value = [
            pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, unicode='\r')
        ]
        esper.process() # Запускаем InputSystem

        self.assertFalse(self.outgoing_queue.empty())
        command = self.outgoing_queue.get_nowait()
        self.assertEqual(command["type"], "CHAT_MESSAGE")
        self.assertEqual(command["payload"]["text"], "Hello World")
        self.assertEqual(self.chat_input.text, "", "Поле ввода должно очиститься после отправки")

    def test_back_button_in_lobby_disconnects(self):
        """Проверяет, что кнопка 'Назад' в лобби вызывает колбэк отключения."""
        self.client_state.game_phase = "LOBBY"
        self.client_state.my_player_id = 1
        self.client_state.lobby_state = {"1": {"ready": False}}
        esper.process() # Создаем UI

        back_button = next(b for b in self.ui_manager.elements if b.text == "Назад")
        back_button.callback()

        self.mock_disconnect.assert_called_once()