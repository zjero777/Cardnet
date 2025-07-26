import esper
import time
import random
from src.common.components import (
    PlayCardCommand, Player, CardInfo, InHand, OnBoard, Owner, ActiveTurn, EndTurnCommand,
    Deck, InDeck, GameOver, SpellEffect, Tapped, TapLandCommand, PlayedLandThisTurn, SummoningSickness,
    Attacking, WaitingForBlockers, DeclareBlockersCommand, DeclareAttackersCommand,
    Graveyard, InGraveyard, KeptHand,
    MulliganCommand, KeepHandCommand, PutCardsBottomCommand, MulliganDecisionPhase,
    MulliganCount, GamePhaseComponent
)

def _move_to_graveyard(card_ent, event_queue):
    """Перемещает карту на кладбище ее владельца."""
    if not esper.entity_exists(card_ent): return
    try:
        owner = esper.component_for_entity(card_ent, Owner)
        player_ent = owner.player_entity_id
        graveyard = esper.component_for_entity(player_ent, Graveyard)
        graveyard.card_ids.append(card_ent)

        if esper.has_component(card_ent, OnBoard):
            esper.remove_component(card_ent, OnBoard)
        if esper.has_component(card_ent, InHand):
            esper.remove_component(card_ent, InHand)

        esper.add_component(card_ent, InGraveyard())
        event_queue.append({"type": "CARD_DIED", "payload": {"card_id": card_ent}})
    except KeyError:
        print(f"Could not move card {card_ent} to graveyard, deleting instead.")
        esper.delete_entity(card_ent, immediate=True)
        event_queue.append({"type": "CARD_DIED", "payload": {"card_id": card_ent}})

class PlayCardSystem(esper.Processor):
    """Система, отвечающая за логику розыгрыша карт."""

    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue

    def _send_error(self, player_id, message):
        self.event_queue.append({"type": "ACTION_ERROR", "payload": {"player_id": player_id, "message": message}})

    def _play_land(self, player, command, card_info):
        """Логика розыгрыша земли."""
        if esper.has_component(command.player_entity_id, PlayedLandThisTurn):
            self._send_error(command.player_entity_id, "Вы уже разыграли землю в этом ходу.")
            return

        print(f"Player {command.player_entity_id} plays land {card_info.name}!")
        # Перемещаем на стол
        esper.remove_component(command.card_entity_id, InHand)
        esper.add_component(command.card_entity_id, OnBoard())
        # Земли входят в игру не повернутыми
        # Устанавливаем флаг, что земля была разыграна
        esper.add_component(command.player_entity_id, PlayedLandThisTurn())
        self.event_queue.append({
            "type": "CARD_MOVED",
            "payload": {"card_id": command.card_entity_id, "from": "HAND", "to": "BOARD"}
        })

    def _play_minion(self, player, command, card_info):
        """Логика розыгрыша существа."""
        print(f"Player {command.player_entity_id} plays minion {card_info.name}!")
        # 1. Списываем ману 
        player.mana_pool -= card_info.cost
        self.event_queue.append({
            "type": "PLAYER_MANA_POOL_UPDATED",
            "payload": {"player_id": command.player_entity_id, "new_mana_pool": player.mana_pool}
        })
        # 2. Перемещаем на стол
        esper.remove_component(command.card_entity_id, InHand)
        esper.add_component(command.card_entity_id, OnBoard())
        esper.add_component(command.card_entity_id, SummoningSickness())  # "Болезнь вызова"
        self.event_queue.append({
            "type": "CARD_MOVED",
            "payload": {"card_id": command.card_entity_id, "from": "HAND", "to": "BOARD"}
        })

    def _play_spell(self, player, command, card_info, spell_effect):
        """Логика розыгрыша заклинания."""
        print(f"Player {command.player_entity_id} plays spell {card_info.name}!")

        # Проверка цели
        if spell_effect.requires_target and command.target_id is None:
            self._send_error(command.player_entity_id, f"Заклинание '{card_info.name}' требует цель.")
            return

        if not spell_effect.requires_target and command.target_id is not None:
            self._send_error(command.player_entity_id, f"Заклинание '{card_info.name}' не использует цель.")
            return

        # Списываем ману
        player.mana_pool -= card_info.cost
        self.event_queue.append({
            "type": "PLAYER_MANA_POOL_UPDATED",
            "payload": {"player_id": command.player_entity_id, "new_mana_pool": player.mana_pool}
        })

        # Применяем эффект
        if spell_effect.effect_type == "DEAL_DAMAGE":
            target_ent = command.target_id
            if not esper.entity_exists(target_ent):
                self._send_error(command.player_entity_id, "Цель не существует.")
                # Возвращаем ману, т.к. действие не удалось
                player.mana_pool += card_info.cost
                return

            # Наносим урон игроку
            if esper.has_component(target_ent, Player):
                target_player = esper.component_for_entity(target_ent, Player)
                target_player.health -= spell_effect.value
                self.event_queue.append({
                    "type": "PLAYER_DAMAGED",
                    "payload": {
                        "player_id": target_ent,
                        "new_health": target_player.health,
                        "source_card_id": command.card_entity_id
                    }
                })
                if target_player.health <= 0 and not esper.get_component(GameOver):
                    esper.create_entity(GameOver(winner_player_id=command.player_entity_id))
            # Наносим урон существу
            elif esper.has_component(target_ent, CardInfo):
                target_card = esper.component_for_entity(target_ent, CardInfo)
                target_card.health -= spell_effect.value
                # Можно добавить новое событие CARD_DAMAGED или просто положиться на FULL_STATE_UPDATE
                if target_card.health <= 0:
                    _move_to_graveyard(target_ent, self.event_queue)
            else:
                self._send_error(command.player_entity_id, "Неверный тип цели для урона.")
                player.mana_pool += card_info.cost # Возврат маны
                return

        # Перемещаем заклинание на кладбище после использования
        _move_to_graveyard(command.card_entity_id, self.event_queue)

    def process(self):
        for command_ent, command in list(esper.get_component(PlayCardCommand)):
            print(f"Processing PlayCardCommand for player {command.player_entity_id} to play card {command.card_entity_id}", flush=True)
            try:
                # --- Общие проверки ---
                if not esper.has_component(command.player_entity_id, ActiveTurn):
                    self._send_error(command.player_entity_id, "Не ваш ход.")
                    continue

                player = esper.component_for_entity(command.player_entity_id, Player)
                card_info = esper.component_for_entity(command.card_entity_id, CardInfo)
                owner = esper.component_for_entity(command.card_entity_id, Owner)

                if owner.player_entity_id != command.player_entity_id:
                    self._send_error(command.player_entity_id, "Это не ваша карта.")
                    continue

                if not esper.has_component(command.card_entity_id, InHand):
                    self._send_error(command.player_entity_id, "Карта не в руке.")
                    continue
                
                # Земли бесплатны, для остальных проверяем ману
                if card_info.card_type != "LAND" and player.mana_pool < card_info.cost:
                    self._send_error(command.player_entity_id, f"Недостаточно маны. Нужно {card_info.cost}, у вас {player.mana_pool}.")
                    continue

                # --- Разделение логики по типу карты ---
                if card_info.card_type == "LAND":
                    self._play_land(player, command, card_info)
                elif card_info.card_type == "MINION":
                    self._play_minion(player, command, card_info)
                elif card_info.card_type == "SPELL":
                    spell_effect = esper.component_for_entity(command.card_entity_id, SpellEffect)
                    self._play_spell(player, command, card_info, spell_effect)
                else:
                    self._send_error(command.player_entity_id, f"Неизвестный тип карты: {card_info.card_type}")

            except KeyError as e:
                print(f"Error processing command: a required entity or component was not found. {e}")
                self._send_error(command.player_entity_id, "Ошибка: сущность или компонент не найден.")
            finally:
                esper.delete_entity(command_ent, immediate=True)


class TurnManagementSystem(esper.Processor):
    """Система управления ходами, маной и началом/концом хода."""

    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue

    def process(self):
        # 1. Обработка команд на завершение хода
        for cmd_ent, command in list(esper.get_component(EndTurnCommand)):
            active_player_ent = -1
            # Находим активного игрока
            for ent, _ in esper.get_component(ActiveTurn):
                active_player_ent = ent
                break

            # Проверяем, что команду отправил активный игрок
            if command.player_entity_id == active_player_ent:
                print(f"Player {active_player_ent} ends their turn.")
                all_players = [ent for ent, _ in esper.get_component(Player)]
                self._end_turn_for(active_player_ent, all_players)

            # Удаляем команду
            esper.delete_entity(cmd_ent, immediate=True)

    def _end_turn_for(self, active_player_ent, all_players):
        """Helper function to contain the logic for ending a turn and starting a new one."""
        # --- CLEANUP STEP for the active player ---
        # Heal all creatures controlled by the active player.
        # This happens at the end of their turn.
        for card_ent, (owner, card_info, _) in list(esper.get_components(Owner, CardInfo, OnBoard)):
            if owner.player_entity_id == active_player_ent:
                if card_info.card_type == "MINION" and card_info.max_health is not None:
                    if card_info.health < card_info.max_health:
                        print(f"Healing card {card_ent} ({card_info.name}) from {card_info.health} to {card_info.max_health}")
                        card_info.health = card_info.max_health

        # Завершаем ход текущего игрока
        esper.remove_component(active_player_ent, ActiveTurn)
        self.event_queue.append({"type": "TURN_ENDED", "payload": {"player_id": active_player_ent}})

        # Находим следующего игрока
        next_player_index = (all_players.index(active_player_ent) + 1) % len(all_players)
        next_player_ent = all_players[next_player_index]

        # --- НАЧАЛО ХОДА СЛЕДУЮЩЕГО ИГРОКА ---
        esper.add_component(next_player_ent, ActiveTurn())
        next_player_component = esper.component_for_entity(next_player_ent, Player)
        # 1. Фаза разворота (Untap Step)
        # Разворачиваем все перманенты (карты на столе), которые контролирует игрок
        for card_ent, (owner, _) in list(esper.get_components(Owner, Tapped)):
            if owner.player_entity_id == next_player_ent and esper.has_component(card_ent, OnBoard):
                esper.remove_component(card_ent, Tapped)
                print(f"Card {card_ent} untapped for player {next_player_ent}")

        # 1.5. Фаза "пробуждения" (снимаем болезнь вызова)
        # Итерируемся по копии, т.к. будем изменять компоненты
        for card_ent, (owner, _) in list(esper.get_components(Owner, SummoningSickness)):
            if owner.player_entity_id == next_player_ent:
                esper.remove_component(card_ent, SummoningSickness)
                print(f"Card {card_ent} no longer has summoning sickness for player {next_player_ent}")

        # 2. Сбрасываем флаг розыгрыша земли
        if esper.has_component(next_player_ent, PlayedLandThisTurn):
            esper.remove_component(next_player_ent, PlayedLandThisTurn)

        # 3. Очищаем пул маны
        next_player_component.mana_pool = 0

        self.event_queue.append({"type": "TURN_STARTED", "payload": {"player_id": next_player_ent}})
        self.event_queue.append({
            "type": "PLAYER_MANA_POOL_UPDATED",
            "payload": {"player_id": next_player_ent, "new_mana_pool": 0}
        })

        # Логика взятия карты
        try:
            player_deck = esper.component_for_entity(next_player_ent, Deck)
            if player_deck.card_ids:
                card_to_draw_id = player_deck.card_ids.pop(0) # Берем верхнюю карту
                esper.remove_component(card_to_draw_id, InDeck)
                esper.add_component(card_to_draw_id, InHand())
                self.event_queue.append({
                    "type": "CARD_DRAWN",
                    "payload": {"player_id": next_player_ent, "card_id": card_to_draw_id}
                })
                print(f"Player {next_player_ent} draws a card.")
            else:
                print(f"Player {next_player_ent} has no cards left to draw.")
        except KeyError:
            print(f"Player {next_player_ent} has no Deck component.")


class GameOverSystem(esper.Processor):
    """Система для проверки условия конца игры и объявления победителя."""
    RESET_DELAY = 5.0  # 5 секунд до сброса

    def __init__(self, event_queue, reset_callback):
        super().__init__()
        self.event_queue = event_queue
        self.reset_callback = reset_callback
        self.game_is_over = False
        self.game_over_time = None

    def process(self):
        # 1. Если игра окончена, проверяем, не пора ли ее сбросить
        if self.game_is_over and self.game_over_time:
            if time.monotonic() - self.game_over_time > self.RESET_DELAY:
                print("--- Resetting game state after delay ---")
                self.reset_callback()
                self.game_is_over = False
                self.game_over_time = None
            return  # Ничего не делаем, пока ждем сброса

        # 2. Если игра уже помечена как завершенная (но таймер еще не запущен), выходим
        if self.game_is_over:
            return

        # 3. Ищем компонент-маркер GameOver, чтобы запустить процесс завершения игры
        for _, game_over in esper.get_component(GameOver):
            self.game_is_over = True
            self.game_over_time = time.monotonic()  # Запускаем таймер сброса
            winner_id = game_over.winner_player_id
            # Простая логика определения проигравшего для 2 игроков
            all_players = [ent for ent, _ in esper.get_component(Player)]
            loser_id = next((p for p in all_players if p != winner_id), None)
            print(f"GAME OVER! Winner is Player {winner_id}")
            self.event_queue.append({
                "type": "GAME_OVER",
                "payload": {"winner_id": winner_id, "loser_id": loser_id}
            })
            # Прерываем, чтобы событие отправилось только один раз
            break


class AttackSystem(esper.Processor):
    """Система, отвечающая за логику атаки существ."""

    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue

    def _send_error(self, player_id, message):
        self.event_queue.append({"type": "ACTION_ERROR", "payload": {"player_id": player_id, "message": message}})

    def _handle_declare_attackers(self, command: DeclareAttackersCommand):
        """Обрабатывает объявление атакующих от игрока."""
        player_ent = command.player_entity_id

        if not esper.has_component(player_ent, ActiveTurn):
            self._send_error(player_ent, "Не ваш ход.")
            return

        valid_attackers = []
        for attacker_ent in command.attacker_ids:
            if not esper.entity_exists(attacker_ent) or esper.component_for_entity(attacker_ent, Owner).player_entity_id != player_ent:
                continue
            if esper.has_component(attacker_ent, Tapped) or esper.has_component(attacker_ent, SummoningSickness):
                continue
            valid_attackers.append(attacker_ent)

        if not valid_attackers:
            # Если атакующих не выбрано (или все оказались невалидными),
            # бой сразу же завершается. Отправляем событие, которое переведет
            # клиента в следующую фазу (MAIN_2).
            self.event_queue.append({"type": "COMBAT_RESOLVED"})
            return

        for attacker_ent in valid_attackers:
            esper.add_component(attacker_ent, Attacking())

            esper.add_component(attacker_ent, Tapped())

        all_players = [ent for ent, _ in esper.get_component(Player)]
        opponent_ent = next((p for p in all_players if p != player_ent), None)
        if opponent_ent:
            esper.add_component(opponent_ent, WaitingForBlockers())
            self.event_queue.append({
                "type": "BLOCKERS_PHASE_STARTED",
                "payload": {"attackers": valid_attackers}
            })

    def _resolve_combat(self, command: DeclareBlockersCommand):
        """Обрабатывает блоки и рассчитывает урон."""
        declarer_ent = command.player_entity_id
        if not esper.has_component(declarer_ent, WaitingForBlockers):
            self._send_error(declarer_ent, "Сейчас не ваша фаза блокирования.")
            return

        # The player who declared blockers is the defender. The active player is the attacker.
        active_player_ent = next((ent for ent, _ in esper.get_component(ActiveTurn)), None)
        if not active_player_ent:
            return

        esper.remove_component(declarer_ent, WaitingForBlockers)

        # Собираем информацию о всех атакующих и их блокерах
        combat_map = {ent: [] for ent, _ in esper.get_components(Attacking)}

        for blocker_id, attacker_id in command.blocks.items():
            try:
                # Валидация блока
                if attacker_id not in combat_map:
                    self._send_error(declarer_ent, f"Существо {attacker_id} не атакует.")
                    continue
                blocker_owner = esper.component_for_entity(blocker_id, Owner)
                if blocker_owner.player_entity_id != declarer_ent:
                    self._send_error(declarer_ent, f"Существо {blocker_id} не ваше.")
                    continue
                if esper.has_component(blocker_id, Tapped):
                    self._send_error(declarer_ent, f"Существо {blocker_id} повернуто.")
                    continue

                # Блок валиден, поворачиваем блокера
                esper.add_component(blocker_id, Tapped())
                combat_map[attacker_id].append(blocker_id)
            except KeyError:
                self._send_error(declarer_ent, "Ошибка при назначении блокера.")

        # Расчет урона
        defender_player_comp = esper.component_for_entity(declarer_ent, Player)

        for attacker_ent, blocker_ents in combat_map.items():
            if not esper.entity_exists(attacker_ent): continue
            attacker_info = esper.component_for_entity(attacker_ent, CardInfo)

            if not blocker_ents:
                # Атака прошла без блока
                print(f"Attacker {attacker_ent} is unblocked, dealing {attacker_info.attack} damage to player {declarer_ent}")
                defender_player_comp.health -= attacker_info.attack
                self.event_queue.append({
                    "type": "PLAYER_DAMAGED",
                    "payload": {"player_id": declarer_ent, "new_health": defender_player_comp.health, "attacker_id": attacker_ent}
                })
            else:
                # Атака заблокирована
                # TODO: Обработать урон от нескольких блокеров. Пока считаем, что блокер один.
                blocker_ent = blocker_ents[0]
                if not esper.entity_exists(blocker_ent): continue

                blocker_info = esper.component_for_entity(blocker_ent, CardInfo)
                print(f"Attacker {attacker_ent} ({attacker_info.attack}/{attacker_info.health}) blocked by {blocker_ent} ({blocker_info.attack}/{blocker_info.health})")

                blocker_info.health -= attacker_info.attack
                attacker_info.health -= blocker_info.attack
                self.event_queue.append({
                    "type": "CARD_ATTACKED",
                    "payload": {
                        "attacker_id": attacker_ent, "target_id": blocker_ent,
                        "attacker_new_health": attacker_info.health, "target_new_health": blocker_info.health
                    }})

                if esper.entity_exists(blocker_ent) and blocker_info.health <= 0:
                    _move_to_graveyard(blocker_ent, self.event_queue)

            if esper.entity_exists(attacker_ent) and attacker_info.health <= 0:
                _move_to_graveyard(attacker_ent, self.event_queue)

        # Очистка состояния боя
        for attacker_ent in combat_map:
            if esper.entity_exists(attacker_ent):
                esper.remove_component(attacker_ent, Attacking)

        self.event_queue.append({"type": "COMBAT_RESOLVED"})

        # Проверка на конец игры
        if defender_player_comp.health <= 0 and not esper.get_component(GameOver):
            esper.create_entity(GameOver(winner_player_id=active_player_ent))

    def process(self):
        # 1. Обработка объявления атакующих
        for cmd_ent, command in list(esper.get_component(DeclareAttackersCommand)):
            try:
                self._handle_declare_attackers(command)
            except KeyError as e:
                print(f"Error processing declare attackers command: {e}")
                self._send_error(command.player_entity_id, "Существо не найдено")
            finally:
                esper.delete_entity(cmd_ent, immediate=True)

        # 2. Обработка объявления блокеров
        for cmd_ent, command in list(esper.get_component(DeclareBlockersCommand)):
            try:
                self._resolve_combat(command)
            except KeyError as e:
                print(f"Error processing declare blockers command: {e}")
                self._send_error(command.player_entity_id, "Ошибка при обработке блокеров.")
            finally:
                esper.delete_entity(cmd_ent, immediate=True)


class TapLandSystem(esper.Processor):
    """Система для 'поворота' земель для получения маны."""

    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue

    def _send_error(self, player_id, message):
        self.event_queue.append({"type": "ACTION_ERROR", "payload": {"player_id": player_id, "message": message}})

    def process(self):
        for cmd_ent, command in list(esper.get_component(TapLandCommand)):
            player_ent = command.player_entity_id
            card_ent = command.card_entity_id
            try:
                if not esper.has_component(player_ent, ActiveTurn):
                    self._send_error(player_ent, "Не ваш ход.")
                    continue

                card_info = esper.component_for_entity(card_ent, CardInfo)
                if card_info.card_type != "LAND":
                    self._send_error(player_ent, "Это не земля.")
                    continue

                if esper.has_component(card_ent, Tapped):
                    self._send_error(player_ent, "Эта земля уже повернута.")
                    continue

                # Все проверки пройдены, поворачиваем для получения маны
                player = esper.component_for_entity(player_ent, Player)
                player.mana_pool += 1  # Базовые земли дают 1 ману
                esper.add_component(card_ent, Tapped())

                self.event_queue.append({
                    "type": "PLAYER_MANA_POOL_UPDATED",
                    "payload": {"player_id": player_ent, "new_mana_pool": player.mana_pool}
                })
            except KeyError:
                self._send_error(player_ent, "Ошибка: сущность или компонент не найден.")
            finally:
                esper.delete_entity(cmd_ent, immediate=True)


class MulliganSystem(esper.Processor):
    """Обрабатывает фазу муллигана в начале игры."""

    def __init__(self, event_queue):
        super().__init__()
        self.event_queue = event_queue

    def _start_game(self):
        """Начинает основную игру после завершения фазы муллигана."""
        for ent, phase_comp in esper.get_component(GamePhaseComponent):
            phase_comp.phase = "GAME_RUNNING"
            break

        # Игрок 1 начинает
        starting_player_ent = 1
        esper.add_component(starting_player_ent, ActiveTurn())
        self.event_queue.append({"type": "TURN_STARTED", "payload": {"player_id": starting_player_ent}})
        print("--- Mulligan phase complete. Starting game. ---")

    def process(self):
        # Система активна только в фазе MULLIGAN
        # Since GamePhaseComponent is a singleton, we can get it directly.
        game_phase_components = esper.get_component(GamePhaseComponent)
        if not game_phase_components or game_phase_components[0][1].phase != "MULLIGAN":
            return

        # --- Обработка команды "Mulligan" ---
        for cmd_ent, command in list(esper.get_component(MulliganCommand)):
            player_ent = command.player_entity_id
            if not esper.has_component(player_ent, MulliganDecisionPhase):
                continue

            mulligan_counter = esper.component_for_entity(player_ent, MulliganCount)
            mulligan_counter.count += 1
            print(f"Player {player_ent} mulligans for the {mulligan_counter.count} time.")

            # Возвращаем руку в колоду и перемешиваем
            hand_cards = [ent for ent, (owner, _) in esper.get_components(Owner, InHand) if owner.player_entity_id == player_ent]
            deck = esper.component_for_entity(player_ent, Deck)
            for card_ent in hand_cards:
                esper.remove_component(card_ent, InHand)
                esper.add_component(card_ent, InDeck())
                deck.card_ids.append(card_ent)
            random.shuffle(deck.card_ids)

            # Берем 7 новых карт
            for _ in range(7):
                if deck.card_ids:
                    card_to_draw = deck.card_ids.pop(0)
                    esper.remove_component(card_to_draw, InDeck)
                    esper.add_component(card_to_draw, InHand())

            # Переходим к фазе выбора карт для низа колоды
            esper.remove_component(player_ent, MulliganDecisionPhase)
            # Отправляем событие, чтобы главный цикл разослал обновление состояния
            self.event_queue.append({"type": "MULLIGAN_STATE_CHANGED"})
            esper.delete_entity(cmd_ent, immediate=True)

        # --- Обработка команды "Put Cards Bottom" ---
        for cmd_ent, command in list(esper.get_component(PutCardsBottomCommand)):
            player_ent = command.player_entity_id
            mulligan_counter = esper.component_for_entity(player_ent, MulliganCount)

            if len(command.card_ids) != mulligan_counter.count:
                continue # Неверное количество карт

            deck = esper.component_for_entity(player_ent, Deck)
            for card_ent in command.card_ids:
                if esper.has_component(card_ent, InHand) and esper.component_for_entity(card_ent, Owner).player_entity_id == player_ent:
                    esper.remove_component(card_ent, InHand)
                    esper.add_component(card_ent, InDeck())
                    deck.card_ids.append(card_ent) # Добавляем в конец (низ) колоды

            # Возвращаемся к фазе решения
            esper.add_component(player_ent, MulliganDecisionPhase())
            self.event_queue.append({"type": "MULLIGAN_STATE_CHANGED"})
            esper.delete_entity(cmd_ent, immediate=True)

        # --- Обработка команды "Keep Hand" ---
        for cmd_ent, command in list(esper.get_component(KeepHandCommand)):
            player_ent = command.player_entity_id
            if esper.has_component(player_ent, MulliganDecisionPhase):
                print(f"Player {player_ent} keeps their hand.")
                esper.remove_component(player_ent, MulliganDecisionPhase)
                esper.add_component(player_ent, KeptHand())
            self.event_queue.append({"type": "MULLIGAN_STATE_CHANGED"})
            esper.delete_entity(cmd_ent, immediate=True)

        # --- Проверка на начало игры ---
        # Игра начинается, когда оба игрока подтвердили свою руку (имеют компонент KeptHand)
        if len(esper.get_component(KeptHand)) == len(esper.get_component(Player)):
            self._start_game()