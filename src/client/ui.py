import pygame
from typing import Callable, Optional, List, Tuple

# --- UI Constants ---
BUTTON_COLOR = (200, 200, 0)
BUTTON_HOVER_COLOR = (220, 220, 0)
BUTTON_PRESSED_COLOR = (180, 180, 0)
BUTTON_TEXT_COLOR = (0, 0, 0)

CONFIRM_BUTTON_COLOR = (0, 100, 200)
CONFIRM_BUTTON_HOVER_COLOR = (0, 120, 220)
CONFIRM_BUTTON_PRESSED_COLOR = (0, 80, 180)
CONFIRM_BUTTON_TEXT_COLOR = (255, 255, 255)

LABEL_COLOR = (200, 200, 255)
TURN_INDICATOR_PLAYER_COLOR = (100, 200, 100)
TURN_INDICATOR_OPPONENT_COLOR = (200, 100, 100)

# --- Menu Button Colors (Indie Style) ---
MENU_BUTTON_BG = (45, 64, 89)
MENU_BUTTON_HOVER = (60, 85, 118)
MENU_BUTTON_PRESSED = (30, 43, 59)
MENU_BUTTON_TEXT = (234, 234, 234)


class UIElement:
    """Базовый класс для всех элементов интерфейса."""
    def __init__(self, rect: pygame.Rect):
        self.rect = rect

    def draw(self, screen: pygame.Surface):
        """Отрисовывает элемент на экране."""
        raise NotImplementedError

    def handle_event(self, event: pygame.event.Event):
        """Обрабатывает событие ввода."""
        pass


class Label(UIElement):
    """Элемент для отображения текста."""
    def __init__(self, text: str, pos: Tuple[int, int], font: pygame.font.Font, color: Tuple[int, int, int] = LABEL_COLOR, center: bool = True):
        self.text = text
        self.font = font
        self.color = color
        self.image = self.font.render(self.text, True, self.color)
        
        rect = self.image.get_rect()
        if center:
            rect.center = pos
        else:
            rect.topleft = pos
        super().__init__(rect)

    def draw(self, screen: pygame.Surface):
        screen.blit(self.image, self.rect)


class Button(UIElement):
    """Кликабельная кнопка с текстом."""
    def __init__(self, text: str, rect: pygame.Rect, font: pygame.font.Font, callback: Callable, 
                 bg_color=BUTTON_COLOR, hover_color=BUTTON_HOVER_COLOR, pressed_color=BUTTON_PRESSED_COLOR, text_color=BUTTON_TEXT_COLOR):
        super().__init__(rect)
        self.text = text
        self.font = font
        self.callback = callback
        
        self.colors = {
            'normal': bg_color,
            'hover': hover_color,
            'pressed': pressed_color,
        }
        self.text_color = text_color
        
        self.is_hovered = False
        self.is_pressed = False

    def draw(self, screen: pygame.Surface):
        color = self.colors['normal']
        if self.is_pressed:
            color = self.colors['pressed']
        elif self.is_hovered:
            color = self.colors['hover']

        pygame.draw.rect(screen, color, self.rect)
        text_surf = self.font.render(self.text, True, self.text_color)
        text_rect = text_surf.get_rect(center=self.rect.center)
        screen.blit(text_surf, text_rect)

    def handle_event(self, event: pygame.event.Event):
        # This method is no longer called directly by the input loop.
        # The UIManager now coordinates the state.
        pass


class UIManager:
    """Управляет всеми UI элементами, их отрисовкой и обработкой событий."""
    def __init__(self):
        self.elements: List[UIElement] = []
        self.active_rect: Optional[pygame.Rect] = None

    def add_element(self, element: UIElement):
        """Добавляет элемент в менеджер."""
        self.elements.append(element)

    def clear_elements(self):
        """Очищает все элементы."""
        self.elements.clear()

    def process_event(self, event: pygame.event.Event) -> bool:
        """
        Обрабатывает одно событие. Возвращает True, если событие было обработано
        UI, иначе False.
        """
        if not hasattr(event, 'pos'):
            return False # Not a mouse event we can process

        mouse_pos = event.pos
        hovered_button = None
        for element in self.elements:
            if isinstance(element, Button) and element.rect.collidepoint(mouse_pos):
                hovered_button = element
                break

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if hovered_button:
                self.active_rect = hovered_button.rect
                return True  # UI "captures" the mouse down event

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.active_rect:
                if hovered_button and hovered_button.rect == self.active_rect:
                    hovered_button.callback()
                self.active_rect = None
                return True # UI "captures" the mouse up event, even if not on a button

        return False

    def draw(self, screen: pygame.Surface):
        """Отрисовывает все элементы."""
        mouse_pos = pygame.mouse.get_pos()
        hovered_rect = None
        for el in self.elements:
            if isinstance(el, Button) and el.rect.collidepoint(mouse_pos):
                hovered_rect = el.rect
                break

        for element in self.elements:
            if isinstance(element, Button):
                element.is_hovered = (element.rect == hovered_rect)
                element.is_pressed = (element.rect == self.active_rect and element.is_hovered)
            element.draw(screen)


class TextInput(UIElement):
    """Элемент для ввода текста."""
    def __init__(self, rect: pygame.Rect, font: pygame.font.Font,
                 text_color=BUTTON_TEXT_COLOR,
                 bg_color=MENU_BUTTON_PRESSED,
                 active_color=MENU_BUTTON_HOVER,
                 max_len=50):
        super().__init__(rect)
        self.font = font
        self.text_color = text_color
        self.bg_color = bg_color
        self.active_color = active_color
        self.text = ""
        self.is_active = False
        self.cursor_visible = True
        self.cursor_timer = 0.0
        self.max_len = max_len

    def draw(self, screen: pygame.Surface):
        color = self.active_color if self.is_active else self.bg_color
        pygame.draw.rect(screen, color, self.rect, border_radius=5)
        pygame.draw.rect(screen, (150, 150, 150), self.rect, 1, border_radius=5)

        text_surface = self.font.render(self.text, True, self.text_color)
        screen.blit(text_surface, (self.rect.x + 10, self.rect.y + (self.rect.height - text_surface.get_height()) // 2))

        if self.is_active and self.cursor_visible:
            cursor_pos_x = self.rect.x + 10 + text_surface.get_width()
            if cursor_pos_x > self.rect.right - 5:
                cursor_pos_x = self.rect.right - 5
            pygame.draw.line(screen, self.text_color, (cursor_pos_x, self.rect.top + 8), (cursor_pos_x, self.rect.bottom - 8), 1)

    def update(self, delta_time: float):
        if self.is_active:
            self.cursor_timer += delta_time
            if self.cursor_timer >= 0.5:
                self.cursor_timer = 0
                self.cursor_visible = not self.cursor_visible
