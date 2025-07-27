import unittest
import esper

# Импортируем все необходимые компоненты и системы
from src.common.components import (
    Player, CardInfo, Owner, InHand, OnBoard, ActiveTurn, Tapped, SummoningSickness, Attacking, Deck, InDeck, Graveyard, ManaPool,
    InGraveyard, PlayCardCommand, DeclareAttackersCommand, EndTurnCommand, DeclareBlockersCommand, SpellEffect, TapLandCommand,
    MulliganCommand, KeepHandCommand, MulliganDecisionPhase, KeptHand, MulliganCount, GamePhaseComponent, PutCardsBottomCommand,
    Disconnected
)
from src.server.systems import (PlayCardSystem, AttackSystem, TurnManagementSystem, TapLandSystem, MulliganSystem)


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
        self.player1_id = esper.create_entity(Player(player_id=1, health=30), Graveyard())
        self.player2_id = esper.create_entity(Player(player_id=2, health=30), Graveyard())

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
        esper.component_for_entity(self.player1_id, Player).mana_pool = ManaPool(R=1)
        card_id = esper.create_entity(
            CardInfo(name="Goblin", cost={'R': 1}, attack=1, health=1, max_health=1, card_type="MINION"),
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
        player1_mana = esper.component_for_entity(self.player1_id, Player).mana_pool
        self.assertEqual(player1_mana.R, 0, "Красная мана должна была потратиться")
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
        esper.component_for_entity(self.player1_id, Player).mana_pool = ManaPool(W=1, generic=1)
        esper.add_component(self.player1_id, ActiveTurn())
        card_id = esper.create_entity(
            CardInfo(name="Expensive", cost={'R': 1, 'generic': 2}, attack=5, health=5, max_health=5, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            InHand()
        )

        esper.create_entity(PlayCardCommand(player_entity_id=self.player1_id, card_entity_id=card_id))
        esper.process()
        
        self.assertEqual(esper.component_for_entity(self.player1_id, Player).mana_pool.W, 1, "Мана не должна была измениться")
        self.assertTrue(esper.has_component(card_id, InHand), "Карта должна остаться в руке")
        self.assertIn("Недостаточно маны", str(self.event_queue), "Должно быть сообщение об ошибке")

    def test_play_spell_card_with_target(self):
        """Проверяет розыгрыш заклинания с целью."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        player1 = esper.component_for_entity(self.player1_id, Player)
        player1.mana_pool = ManaPool(R=2, C=2)

        spell_card_id = esper.create_entity(
            CardInfo(name="Fireball", cost={'R': 1, 'generic': 3}, card_type="SPELL"),
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
        player1_mana = esper.component_for_entity(self.player1_id, Player).mana_pool
        self.assertEqual(player1_mana.R, 1, "Должна остаться одна красная мана")
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
            CardInfo(name="Knight", cost={'W': 2}, attack=3, health=3, max_health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()  # Уже на столе, может атаковать
        )

        # Действие: создаем команду атаки на игрока 2
        esper.create_entity(DeclareAttackersCommand(
            player_entity_id=self.player1_id,
            attacker_ids=[attacker_id]
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
            CardInfo(name="Knight", cost={'W': 2}, attack=3, health=3, max_health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()
        )
        # 3. Существо объявляется атакующим
        esper.create_entity(DeclareAttackersCommand(player_entity_id=self.player1_id, attacker_ids=[attacker_id]))
        esper.process()

        # Действие: Игрок 2 (защитник) не объявляет блокеров
        esper.create_entity(DeclareBlockersCommand(player_entity_id=self.player2_id, blocks={}))
        esper.process()

        # Проверки:
        # 1. Здоровье игрока 2 должно уменьшиться
        player2_health = esper.component_for_entity(self.player2_id, Player).health
        self.assertEqual(player2_health, 27, "Здоровье игрока 2 должно было уменьшиться на 3")

        # 2. Атакующее существо больше не должно быть помечено как Attacking
        self.assertFalse(esper.has_component(attacker_id, Attacking), "Существо не должно быть атакующим после боя")

        # 3. Событие об уроне должно быть в очереди
        damage_event = next((e for e in self.event_queue if e['type'] == 'PLAYER_DAMAGED'), None)
        self.assertIsNotNone(damage_event, "Должно быть событие PLAYER_DAMAGED")
        self.assertEqual(damage_event['payload']['player_id'], self.player2_id)

    def test_blocked_attack_both_survive(self):
        """Проверяет бой, в котором оба существа выживают."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        attacker_id = esper.create_entity(
            CardInfo(name="Attacker", cost={'R': 2}, attack=2, health=4, max_health=4, card_type="MINION"),
            Owner(player_entity_id=self.player1_id), OnBoard()
        )
        blocker_id = esper.create_entity(
            CardInfo(name="Blocker", cost={'W': 1}, attack=1, health=3, max_health=3, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard()
        )
        # Действие 1: Объявляем атакующих
        esper.create_entity(DeclareAttackersCommand(player_entity_id=self.player1_id, attacker_ids=[attacker_id]))
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

        # Проверяем событие боя
        attack_event = next((e for e in self.event_queue if e['type'] == 'CARD_ATTACKED'), None)
        self.assertIsNotNone(attack_event, "Должно быть событие CARD_ATTACKED")
        payload = attack_event['payload']
        self.assertEqual(payload['attacker_id'], attacker_id)
        self.assertEqual(payload['target_id'], blocker_id)
        self.assertEqual(payload['attacker_new_health'], 3)
        self.assertEqual(payload['target_new_health'], 1)

    def test_blocked_attack_blocker_dies(self):
        """Проверяет бой, в котором блокер погибает."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        attacker_id = esper.create_entity(
            CardInfo(name="Attacker", cost={'R': 3}, attack=3, health=3, max_health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id), OnBoard()
        )
        blocker_id = esper.create_entity(
            CardInfo(name="Blocker", cost={'W': 1}, attack=1, health=2, max_health=2, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard()
        )
        # Действие 1: Объявляем атакующих
        esper.create_entity(DeclareAttackersCommand(player_entity_id=self.player1_id, attacker_ids=[attacker_id]))
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
        player1.mana_pool = ManaPool()
        land_id = esper.create_entity(
            CardInfo(name="Plains", cost={}, card_type="LAND", produces="W"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()
        )

        # Действие
        esper.create_entity(TapLandCommand(player_entity_id=self.player1_id, card_entity_id=land_id))
        esper.process()

        # Проверки
        self.assertEqual(esper.component_for_entity(self.player1_id, Player).mana_pool.W, 1, "Пул белой маны должен увеличиться на 1")
        self.assertTrue(esper.has_component(land_id, Tapped), "Земля должна быть повернута")


class TestTurnManagementSystem(SystemsTestBase):
    """Тесты для системы управления ходами."""

    def test_turn_end_and_start_flow(self):
        """Проверяет полный цикл завершения хода и начала нового."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        # Даем игроку 2 существо с болезнью вызова и повернутую землю
        esper.create_entity(
            CardInfo(name="Tapped Land", cost={}, card_type="LAND"),
            Owner(player_entity_id=self.player2_id), OnBoard(), Tapped()
        )
        esper.create_entity(
            CardInfo(name="Sick Minion", cost={'R': 1}, attack=1, health=1, max_health=1, card_type="MINION"),
            Owner(player_entity_id=self.player2_id), OnBoard(), SummoningSickness()
        )
        # Даем игроку 2 карту в колоду для взятия
        card_in_deck = esper.create_entity(
            CardInfo(name="Test Card", cost={'R': 1}, attack=1, health=1, max_health=1, card_type="MINION"),
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

    def test_end_of_turn_heals_creatures(self):
        """Проверяет, что существа активного игрока лечатся в конце его хода."""
        # Подготовка
        esper.add_component(self.player1_id, ActiveTurn())
        # Создаем существо с неполным здоровьем
        damaged_minion_id = esper.create_entity(
            CardInfo(name="Damaged Knight", cost={'W': 2}, attack=3, health=1, max_health=3, card_type="MINION"),
            Owner(player_entity_id=self.player1_id),
            OnBoard()
        )
        # Создаем существо оппонента, которое не должно лечиться
        opponent_minion_id = esper.create_entity(
            CardInfo(name="Opponent's Minion", cost={'R': 1}, attack=1, health=1, max_health=2, card_type="MINION"),
            Owner(player_entity_id=self.player2_id),
            OnBoard()
        )

        # Действие: игрок 1 завершает ход
        esper.create_entity(EndTurnCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверки
        healed_minion_info = esper.component_for_entity(damaged_minion_id, CardInfo)
        self.assertEqual(healed_minion_info.health, 3, "Существо активного игрока должно было полностью вылечиться")
        opponent_minion_info = esper.component_for_entity(opponent_minion_id, CardInfo)
        self.assertEqual(opponent_minion_info.health, 1, "Существо оппонента не должно было лечиться")


class TestGameFlow(unittest.TestCase):
    """Тесты для общих игровых механик, таких как муллиган и смена фаз."""

    def setUp(self):
        """Настраивает мир для тестов игрового потока."""
        esper.clear_database()
        self.event_queue = []

        # Создаем игроков
        self.player1_id = esper.create_entity(
            Player(player_id=1, health=30),
            Graveyard(),
            MulliganDecisionPhase(),
            MulliganCount(count=0)
        )
        self.player2_id = esper.create_entity(
            Player(player_id=2, health=30),
            Graveyard(),
            MulliganDecisionPhase(),
            MulliganCount(count=0)
        )
        # Создаем карты и добавляем их ID в колоды
        p1_deck_ids = []
        for i in range(40):
            card_id = esper.create_entity(
                CardInfo(name=f"P1 Card {i}", cost={'R': 1}, attack=1, health=1, max_health=1, card_type="MINION"),
                Owner(self.player1_id), InDeck()
            )
            p1_deck_ids.append(card_id)
        p2_deck_ids = []
        for i in range(40):
            card_id = esper.create_entity(
                CardInfo(name=f"P2 Card {i}", cost={'W': 1}, attack=1, health=1, max_health=1, card_type="MINION"),
                Owner(self.player2_id), InDeck()
            )
            p2_deck_ids.append(card_id)

        esper.add_component(self.player1_id, Deck(card_ids=p1_deck_ids))
        esper.add_component(self.player2_id, Deck(card_ids=p2_deck_ids))

        # Синглтон-компонент для фазы игры
        esper.create_entity(GamePhaseComponent(phase="MULLIGAN"))

        # Добавляем системы
        esper.add_processor(MulliganSystem(self.event_queue))
        esper.add_processor(TurnManagementSystem(self.event_queue))

    def tearDown(self):
        """Очищает мир и процессоры после каждого теста."""
        esper.clear_database()
        esper._processors.clear()

    def test_mulligan_phase_to_game_running(self):
        """Проверяет переход из фазы муллигана в основную фазу игры."""
        game_phase_entity = esper.get_component(GamePhaseComponent)[0][0]
        self.assertEqual(esper.component_for_entity(game_phase_entity, GamePhaseComponent).phase, "MULLIGAN")

        esper.create_entity(KeepHandCommand(player_entity_id=self.player1_id))
        esper.process()

        self.assertTrue(esper.has_component(self.player1_id, KeptHand))
        self.assertEqual(esper.component_for_entity(game_phase_entity, GamePhaseComponent).phase, "MULLIGAN")

        esper.create_entity(KeepHandCommand(player_entity_id=self.player2_id))
        esper.process()

        self.assertEqual(esper.component_for_entity(game_phase_entity, GamePhaseComponent).phase, "GAME_RUNNING")
        p1_active = esper.has_component(self.player1_id, ActiveTurn)
        p2_active = esper.has_component(self.player2_id, ActiveTurn)
        self.assertTrue(p1_active or p2_active, "У одного из игроков должен был начаться ход")

    def _deal_hand(self, player_id, num_cards=7):
        """Вспомогательная функция для раздачи карт в руку из колоды."""
        deck = esper.component_for_entity(player_id, Deck)
        for _ in range(num_cards):
            if deck.card_ids:
                card_to_draw = deck.card_ids.pop(0)
                if esper.has_component(card_to_draw, InDeck):
                    esper.remove_component(card_to_draw, InDeck)
                esper.add_component(card_to_draw, InHand())

    def test_mulligan_shuffles_deck(self):
        """Проверяет, что при муллигане рука возвращается в колоду, и колода перемешивается."""
        # Подготовка: раздаем стартовую руку
        self._deal_hand(self.player1_id, 7)
        deck_comp = esper.component_for_entity(self.player1_id, Deck)
        hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]

        # Сохраняем состояние до муллигана
        deck_before_mulligan = list(deck_comp.card_ids)
        full_deck_before_mulligan = deck_before_mulligan + hand_cards

        # Действие: игрок берет муллиган
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверки
        new_deck_comp = esper.component_for_entity(self.player1_id, Deck)
        new_hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]

        self.assertEqual(len(new_hand_cards), 7, "Игрок должен взять 7 новых карт")
        self.assertEqual(len(new_deck_comp.card_ids), 33, "В колоде должно остаться 33 карты")

        # Проверяем, что состав колоды не изменился (только порядок)
        full_deck_after_mulligan = new_deck_comp.card_ids + new_hand_cards
        self.assertCountEqual(full_deck_before_mulligan, full_deck_after_mulligan, "Общий набор карт должен остаться тем же")

        # Проверяем, что порядок изменился (вероятностный тест)
        self.assertNotEqual(full_deck_before_mulligan, full_deck_after_mulligan, "Порядок карт в колоде должен был измениться после перемешивания")

    def test_put_cards_bottom_after_mulligan(self):
        """Проверяет, что после муллигана игрок кладет карты в низ колоды."""
        # Подготовка: раздаем руку, игрок 1 берет один муллиган и решает оставить руку
        self._deal_hand(self.player1_id, 7)
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()
        esper.create_entity(KeepHandCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверка состояния: игрок должен быть в фазе "положить карты вниз"
        self.assertFalse(esper.has_component(self.player1_id, MulliganDecisionPhase))
        self.assertFalse(esper.has_component(self.player1_id, KeptHand), "Не должен получить KeptHand до выбора карт")

        hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        card_to_put_bottom = hand_cards[0]
        deck_comp = esper.component_for_entity(self.player1_id, Deck)
        deck_size_before = len(deck_comp.card_ids)

        # Действие: игрок кладет одну карту вниз колоды
        esper.create_entity(PutCardsBottomCommand(player_entity_id=self.player1_id, card_ids=[card_to_put_bottom]))
        esper.process()

        # Проверки
        self.assertTrue(esper.has_component(self.player1_id, KeptHand), "Должен получить KeptHand после выбора карт")
        new_hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        self.assertEqual(len(new_hand_cards), 6, "В руке должно остаться 6 карт")
        self.assertNotIn(card_to_put_bottom, new_hand_cards, "Выбранная карта должна уйти из руки")
        new_deck_comp = esper.component_for_entity(self.player1_id, Deck)
        self.assertEqual(len(new_deck_comp.card_ids), deck_size_before + 1, "Карта должна добавиться в колоду")
        self.assertEqual(new_deck_comp.card_ids[-1], card_to_put_bottom, "Карта должна быть последней в колоде")

    def test_cannot_mulligan_with_empty_hand(self):
        """Проверяет, что игрок не может взять муллиган с пустой рукой."""
        # Подготовка: раздаем руку, а затем забираем все карты
        self._deal_hand(self.player1_id, 7)
        hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]

        # Забираем карты из руки
        for card_ent in hand_cards:
            esper.remove_component(card_ent, InHand)

        mulligan_count_before = esper.component_for_entity(self.player1_id, MulliganCount).count

        # Действие: пытаемся взять муллиган с пустой рукой
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверки
        mulligan_count_after = esper.component_for_entity(self.player1_id, MulliganCount).count
        self.assertEqual(mulligan_count_after, mulligan_count_before, "Счетчик муллиганов не должен был увеличиться")
        final_hand = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        self.assertEqual(len(final_hand), 0, "Игрок не должен был взять новые карты")
        self.assertTrue(esper.has_component(self.player1_id, MulliganDecisionPhase), "Игрок должен остаться в фазе решения о муллигане")

    def test_mulligan_action(self):
        """Проверяет, что команда муллигана увеличивает счетчик."""
        # Сначала нужно раздать руку, т.к. нельзя сделать муллиган с пустой рукой
        self._deal_hand(self.player1_id, 7)
        self.assertEqual(esper.component_for_entity(self.player1_id, MulliganCount).count, 0)
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()
        self.assertEqual(esper.component_for_entity(self.player1_id, MulliganCount).count, 1)
        self.assertTrue(esper.has_component(self.player1_id, MulliganDecisionPhase))

    def test_cannot_mulligan_if_not_enough_cards_total(self):
        """Проверяет, что игрок не может взять муллиган, если в сумме в руке и колоде меньше 7 карт."""
        # Подготовка:
        # 1. Уменьшаем колоду игрока до 6 карт.
        deck_comp = esper.component_for_entity(self.player1_id, Deck)
        deck_comp.card_ids = deck_comp.card_ids[:6]

        # 2. Раздаем эти 6 карт в руку. В колоде 0 карт.
        self._deal_hand(self.player1_id, 6)

        hand_after_dealing = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        self.assertEqual(len(hand_after_dealing), 6)
        self.assertEqual(len(deck_comp.card_ids), 0)

        mulligan_count_before = esper.component_for_entity(self.player1_id, MulliganCount).count

        # Действие: пытаемся взять муллиган. В сумме 6 карт, должно провалиться.
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверки:
        mulligan_count_after = esper.component_for_entity(self.player1_id, MulliganCount).count
        self.assertEqual(mulligan_count_after, mulligan_count_before, "Счетчик муллиганов не должен был увеличиться")

        final_hand = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        self.assertCountEqual(final_hand, hand_after_dealing, "Рука не должна была измениться")

        error_event = next((e for e in self.event_queue if e['type'] == 'ACTION_ERROR'), None)
        self.assertIsNotNone(error_event, "Должно быть событие ACTION_ERROR")
        self.assertEqual(error_event['payload']['message'], "Недостаточно карт в колоде для муллигана.")

    def test_mulligan_count_limit(self):
        """Проверяет, что игрок не может взять муллиган, если это приведет к невозможности завершить фазу."""
        # Подготовка:
        # 1. Раздаем руку игроку.
        self._deal_hand(self.player1_id, 7)

        # 2. Устанавливаем счетчик муллиганов на 6.
        mulligan_counter = esper.component_for_entity(self.player1_id, MulliganCount)
        mulligan_counter.count = 6

        # Действие 1: Берем 7-й муллиган. Это должно сработать.
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверка 1:
        self.assertEqual(esper.component_for_entity(self.player1_id, MulliganCount).count, 7, "Счетчик муллиганов должен стать 7")
        hand_after_mulligan = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == self.player1_id]
        self.assertEqual(len(hand_after_mulligan), 7, "Игрок должен был взять 7 новых карт")

        # Действие 2: Пытаемся взять 8-й муллиган. Это должно провалиться.
        esper.create_entity(MulliganCommand(player_entity_id=self.player1_id))
        esper.process()

        # Проверка 2:
        self.assertEqual(esper.component_for_entity(self.player1_id, MulliganCount).count, 7, "Счетчик муллиганов не должен был увеличиться")
        error_event = next((e for e in self.event_queue if e['type'] == 'ACTION_ERROR'), None)
        self.assertIsNotNone(error_event, "Должно быть событие ACTION_ERROR")
        self.assertEqual(error_event['payload']['message'], "Нельзя взять больше муллиганов.")

    def test_game_does_not_start_if_player_disconnected(self):
        """Проверяет, что игра не начинается, если один из игроков отключен во время муллигана."""
        # Подготовка: игрок 1 подтверждает руку, а игрок 2 отключается
        esper.create_entity(KeepHandCommand(player_entity_id=self.player1_id))
        esper.process()

        # Добавляем компонент Disconnected игроку 2
        esper.add_component(self.player2_id, Disconnected())

        # Действие: игрок 2 (уже отключенный) отправляет команду KeepHand (это может случиться, если команда была в пути)
        esper.create_entity(KeepHandCommand(player_entity_id=self.player2_id))
        esper.process()

        # Проверки:
        game_phase_entity = esper.get_component(GamePhaseComponent)[0][0]
        self.assertEqual(esper.component_for_entity(game_phase_entity, GamePhaseComponent).phase, "MULLIGAN",
                         "Игра должна оставаться в фазе муллигана, если кто-то отключен")

        # Теперь симулируем переподключение игрока 2
        esper.remove_component(self.player2_id, Disconnected)
        esper.process()  # Запускаем цикл еще раз, чтобы система проверила состояние

        # Проверка после переподключения:
        self.assertEqual(esper.component_for_entity(game_phase_entity, GamePhaseComponent).phase, "GAME_RUNNING",
                         "Игра должна начаться после переподключения всех игроков")