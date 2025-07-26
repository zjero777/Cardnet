import unittest
import esper

# Импортируем все необходимые компоненты и системы
from src.common.components import (
    Player, CardInfo, Owner, InHand, OnBoard, ActiveTurn, Tapped, SummoningSickness, Attacking, Deck, InDeck, Graveyard, InGraveyard,
    PlayCardCommand, AttackCommand, EndTurnCommand, DeclareBlockersCommand, SpellEffect, TapLandCommand
)
from src.server.systems import PlayCardSystem, AttackSystem, TurnManagementSystem, TapLandSystem


class SystemsTestBase(unittest.TestCase):
    """
    Базовый класс для тестов систем. Создает новый мир esper для каждого теста,
    двух игроков и добавляет необходимые системы.
    """
    def setUp(self):
        """Настраивает тестовое окружение перед каждым тестом."""
        esper.clear_database()
        self.event_queue = []

        # Создаем игроков
        self.player1_id = esper.create_entity(Player(player_id=1, health=30, mana_pool=10), Graveyard())
        self.player2_id = esper.create_entity(Player(player_id=2, health=30, mana_pool=10), Graveyard())

        # Добавляем системы в мир
        # Порядок важен, чтобы команды обрабатывались корректно
        esper.add_processor(PlayCardSystem(self.event_queue))
        esper.add_processor(AttackSystem(self.event_queue))
        esper.add_processor(TurnManagementSystem(self.event_queue))
        esper.add_processor(TapLandSystem(self.event_queue))

    def tearDown(self):
        """Очищает мир и процессоры после каждого теста."""
        esper.clear_database()
        # Так как esper использует глобальное состояние, процессоры накапливаются
        # между тестами, если их не очищать. Это самая надежная очистка.
        esper._processors.clear()


class TestPlayCardSystem(SystemsTestBase):
    """Тесты для системы розыгрыша карт."""

    def test_play_minion_card_success(self):
        """Проверяет успешный розыгрыш существа."""
        # Подготовка: даем ход игроку 1 и карту в руку
        esper.add_component(self.player1_id, ActiveTurn())
        card_id = esper.create_entity(
            CardInfo(name="Goblin", cost=1, attack=1, health=1, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            InHand()
        )

        # Действие: создаем команду на розыгрыш карты
        esper.create_entity(PlayCardCommand(
            player_entity_id=self.player1_id,
            card_entity_id=card_id
        ))
        esper.process()  # Запускаем обработку систем

        # Проверки:
        player1_mana_pool = esper.component_for_entity(self.player1_id, Player).mana_pool
        self.assertEqual(player1_mana_pool, 9, "Мана должна была потратиться")
        self.assertFalse(esper.has_component(card_id, InHand), "Карта должна уйти из руки")
        self.assertTrue(esper.has_component(card_id, OnBoard), "Карта должна появиться на столе")
        self.assertTrue(esper.has_component(card_id, SummoningSickness), "Существо не может атаковать в ход призыва")

        # Проверяем, что было создано событие для клиентов
        self.assertIn(
            {"type": "CARD_MOVED", "payload": {"card_id": card_id, "from": "HAND", "to": "BOARD"}},
            self.event_queue
        )

    def test_play_card_not_enough_mana(self):
        """Проверяет, что нельзя разыграть карту при нехватке маны."""
        esper.component_for_entity(self.player1_id, Player).mana_pool = 3
        esper.add_component(self.player1_id, ActiveTurn())
        card_id = esper.create_entity(
            CardInfo(name="Expensive", cost=5, attack=5, health=5, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            InHand()
        )

        esper.create_entity(PlayCardCommand(player_entity_id=self.player1_id, card_entity_id=card_id))
        esper.process()

        self.assertEqual(esper.component_for_entity(self.player1_id, Player).mana_pool, 3, "Мана не должна была измениться")
        self.assertTrue(esper.has_component(card_id, InHand), "Карта должна остаться в руке")
        self.assertIn("Недостаточно маны", str(self.event_queue), "Должно быть сообщение об ошибке")

    def test_play_spell_card_with_target(self):
        """Проверяет розыгрыш заклинания с целью."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        player1 = esper.component_for_entity(self.player1_id, Player)
        player1.mana_pool = 5

        spell_card_id = esper.create_entity(
            CardInfo(name="Fireball", cost=4, card_type="SPELL"),
            Owner(player_entity_id=self.player1_id),
            InHand(),
            SpellEffect(effect_type="DEAL_DAMAGE", value=6, requires_target=True)
        )

        # Действие: разыгрываем заклинание в оппонента
        esper.create_entity(PlayCardCommand(
            player_entity_id=self.player1_id,
            card_entity_id=spell_card_id,
            target_id=self.player2_id  # Цель - игрок 2
        ))
        esper.process()

        # Проверки
        self.assertEqual(esper.component_for_entity(self.player1_id, Player).mana_pool, 1, "Мана должна была потратиться")
        self.assertEqual(esper.component_for_entity(self.player2_id, Player).health, 24, "Здоровье оппонента должно было уменьшиться")
        self.assertTrue(esper.has_component(spell_card_id, InGraveyard), "Карта заклинания должна быть на кладбище")
        player1_graveyard = esper.component_for_entity(self.player1_id, Graveyard)
        self.assertIn(spell_card_id, player1_graveyard.card_ids, "ID карты заклинания должен быть в списке кладбища")
        damage_event = next((e for e in self.event_queue if e['type'] == 'PLAYER_DAMAGED'), None)
        self.assertIsNotNone(damage_event, "Должно быть событие PLAYER_DAMAGED")
        self.assertEqual(damage_event['payload']['player_id'], self.player2_id)


class TestAttackSystem(SystemsTestBase):
    """Тесты для системы атаки."""

    def test_declare_attacker(self):
        """Проверяет объявление атакующего существа."""
        # Подготовка: ход игрока 1, у него на столе есть существо без "усталости от призыва"
        esper.add_component(self.player1_id, ActiveTurn())
        attacker_id = esper.create_entity(
            CardInfo(name="Knight", cost=3, attack=3, health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()  # Уже на столе, может атаковать
        )

        # Действие: создаем команду атаки на игрока 2
        esper.create_entity(AttackCommand(
            player_entity_id=self.player1_id,
            attacker_card_id=attacker_id,
            target_card_id=self.player2_id
        ))
        esper.process()

        # Проверки:
        # 1. Существо должно быть помечено как атакующее и повернутое
        self.assertTrue(esper.has_component(attacker_id, Attacking), "Существо должно быть помечено как атакующее")
        self.assertTrue(esper.has_component(attacker_id, Tapped), "Атакующее существо должно быть повернуто")

        # 2. Здоровье игрока НЕ должно измениться на этом этапе
        player2_health = esper.component_for_entity(self.player2_id, Player).health
        self.assertEqual(player2_health, 30, "Здоровье игрока не должно меняться до фазы боя")

    def test_unblocked_attack_deals_damage(self):
        """Проверяет, что незаблокированная атака наносит урон после фазы боя."""
        # Подготовка:
        # 1. Игрок 1 - активный
        esper.add_component(self.player1_id, ActiveTurn())
        # 2. У игрока 1 есть существо на столе
        attacker_id = esper.create_entity(
            CardInfo(name="Knight", cost=3, attack=3, health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()
        )
        # 3. Существо объявляется атакующим
        esper.add_component(attacker_id, Attacking())

        # Действие:
        # 1. Игрок 1 завершает ход, чтобы перейти к фазе боя
        esper.create_entity(EndTurnCommand(player_entity_id=self.player1_id))
        esper.process()

        # 2. Игрок 2 (защитник) не объявляет блокеров
        esper.create_entity(DeclareBlockersCommand(player_entity_id=self.player2_id, blocks={}))
        esper.process()

        # Проверки:
        # 1. Здоровье игрока 2 должно уменьшиться
        player2_health = esper.component_for_entity(self.player2_id, Player).health
        self.assertEqual(player2_health, 27, "Здоровье игрока 2 должно уменьшиться")

        # 2. Атакующее существо больше не должно быть помечено как Attacking
        self.assertFalse(esper.has_component(attacker_id, Attacking), "Существо не должно быть атакующим после боя")

        # 3. Событие об уроне должно быть в очереди
        damage_event_found = any(e['type'] == 'PLAYER_DAMAGED' for e in self.event_queue)
        self.assertTrue(damage_event_found, "Должно быть событие PLAYER_DAMAGED")

    def test_blocked_attack_both_survive(self):
        """Проверяет бой, в котором оба существа выживают."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        attacker_id = esper.create_entity(
            CardInfo(name="Attacker", cost=2, attack=2, health=4, card_type="MINION"),
            Owner(player_entity_id=self.player1_id), OnBoard()
        )
        blocker_id = esper.create_entity(
            CardInfo(name="Blocker", cost=1, attack=1, health=3, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard()
        )
        esper.add_component(attacker_id, Attacking())

        # NEW: Игрок 1 завершает основную фазу, чтобы инициировать бой
        esper.create_entity(EndTurnCommand(player_entity_id=self.player1_id))
        esper.process()

        # Действие: игрок 2 блокирует
        esper.create_entity(DeclareBlockersCommand(player_entity_id=self.player2_id, blocks={blocker_id: attacker_id}))
        esper.process()

        # Проверки
        attacker_info = esper.component_for_entity(attacker_id, CardInfo)
        blocker_info = esper.component_for_entity(blocker_id, CardInfo)
        self.assertEqual(attacker_info.health, 3, "Здоровье атакующего должно уменьшиться на 1")
        self.assertEqual(blocker_info.health, 1, "Здоровье блокера должно уменьшиться на 2")
        self.assertTrue(esper.entity_exists(attacker_id), "Атакующий должен выжить")
        self.assertTrue(esper.entity_exists(blocker_id), "Блокер должен выжить")
        self.assertEqual(esper.component_for_entity(self.player2_id, Player).health, 30, "Здоровье защитника не должно измениться")

    def test_blocked_attack_blocker_dies(self):
        """Проверяет бой, в котором блокер погибает."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        attacker_id = esper.create_entity(
            CardInfo(name="Attacker", cost=3, attack=3, health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id), OnBoard()
        )
        blocker_id = esper.create_entity(
            CardInfo(name="Blocker", cost=1, attack=1, health=2, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard()
        )
        esper.add_component(attacker_id, Attacking())

        # NEW: Игрок 1 завершает основную фазу, чтобы инициировать бой
        esper.create_entity(EndTurnCommand(player_entity_id=self.player1_id))
        esper.process()

        # Действие
        esper.create_entity(DeclareBlockersCommand(player_entity_id=self.player2_id, blocks={blocker_id: attacker_id}))
        esper.process()

        # Проверки
        self.assertEqual(esper.component_for_entity(attacker_id, CardInfo).health, 2)
        self.assertTrue(esper.has_component(blocker_id, InGraveyard), "Блокер должен был переместиться на кладбище")
        player2_graveyard = esper.component_for_entity(self.player2_id, Graveyard)
        self.assertIn(blocker_id, player2_graveyard.card_ids, "ID блокера должен быть в списке кладбища")
        card_died_event = next((e for e in self.event_queue if e['type'] == 'CARD_DIED'), None)
        self.assertIsNotNone(card_died_event, "Должно быть событие CARD_DIED")
        self.assertEqual(card_died_event['payload']['card_id'], blocker_id)


class TestTapLandSystem(SystemsTestBase):
    """Тесты для системы поворота земель."""

    def test_tap_land_for_mana(self):
        """Проверяет, что поворот земли дает ману."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        player1 = esper.component_for_entity(self.player1_id, Player)
        player1.mana_pool = 0
        land_id = esper.create_entity(
            CardInfo(name="Plains", cost=0, card_type="LAND"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()
        )

        # Действие
        esper.create_entity(TapLandCommand(player_entity_id=self.player1_id, card_entity_id=land_id))
        esper.process()

        # Проверки
        self.assertEqual(esper.component_for_entity(self.player1_id, Player).mana_pool, 1, "Пул маны должен увеличиться на 1")
        self.assertTrue(esper.has_component(land_id, Tapped), "Земля должна быть повернута")


class TestTurnManagementSystem(SystemsTestBase):
    """Тесты для системы управления ходами."""

    def test_turn_end_and_start_flow(self):
        """Проверяет полный цикл завершения хода и начала нового."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        # Даем игроку 2 существо с болезнью вызова и повернутую землю
        esper.create_entity(
            CardInfo(name="Tapped Land", cost=0, card_type="LAND"),
            Owner(player_entity_id=self.player2_id), OnBoard(), Tapped()
        )
        esper.create_entity(
            CardInfo(name="Sick Minion", cost=1, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard(), SummoningSickness()
        )
        # Даем игроку 2 карту в колоду для взятия
        card_in_deck = esper.create_entity(
            CardInfo(name="Test Card", cost=1, card_type="MINION"),
            Owner(self.player2_id),
            InDeck()
        )
        esper.add_component(self.player2_id, Deck(card_ids=[card_in_deck]))

        # Действие: игрок 1 завершает ход
        esper.create_entity(EndTurnCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверки для игрока 2 (начало его хода)
        self.assertFalse(esper.has_component(self.player1_id, ActiveTurn), "Ход игрока 1 должен был завершиться")
        self.assertTrue(esper.has_component(self.player2_id, ActiveTurn), "Ход игрока 2 должен был начаться")
        self.assertTrue(esper.has_component(card_in_deck, InHand), "Игрок 2 должен был взять карту")
        
        # Проверяем, что перманенты развернулись и "вылечились"
        for ent, (owner, _) in esper.get_components(Owner, OnBoard):
            if owner.player_entity_id == self.player2_id:
                self.assertFalse(esper.has_component(ent, Tapped), f"Карта {ent} игрока 2 должна была развернуться")
                self.assertFalse(esper.has_component(ent, SummoningSickness), f"Карта {ent} игрока 2 не должна иметь болезнь вызова")