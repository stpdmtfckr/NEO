# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import unittest

from jarvis.gaming.routing_firewall import (
    is_explicit_game_session_query,
    is_explicit_player_state_query,
)


KNOWLEDGE_QUERIES = (
    "Сколько классов есть в Darktide?",
    "Сколько талантов у Veteran?",
    "Сколько урона у этого оружия?",
    "Сколько стойкости даёт этот талант?",
    "Сколько зарядов у способности?",
    "Сколько ресурсов нужно для крафта?",
    "Сколько противников появляется в миссии?",
    "Сколько длится эффект Confirmed Kill?",
    "Сколько секунд действует бафф?",
    "Сколько раз можно использовать способность?",
    "Как долго действует этот эффект?",
    "В какое время появляется босс?",
    "Какие классы есть в игре?",
    "Какие способности у персонажа Veteran?",
    "Какие таланты у персонажа Zealot?",
    "Расскажи про персонажа Veteran",
    "Что делает этот персонаж?",
    "Где найти этот предмет в игре?",
    "Для чего нужен этот талант?",
    "Что даёт этот перк?",
    "Как работает эта механика?",
    "Какая перезарядка у способности?",
    "Какое время восстановления способности?",
    "Что открывает этот талант?",
    "К какому классу относится Confirmed Kill?",
    "Какие перки подходят к этому оружию?",
    "Что выпадает из этого босса?",
    "Где получить это оружие?",
    "Как создать этот предмет?",
    "Что нужно для крафта?",
    "Какая слабость у этого врага?",
    "Чем отличается Veteran от Zealot?",
    "Какие характеристики у этого оружия?",
    "Что изменилось в этом таланте?",
    "Какие эффекты у этой способности?",
    "Какие классы доступны игроку в Darktide?",
    "Какие персонажи есть в игре?",
    "Сколько здоровья у врага?",
    "Сколько брони даёт предмет?",
    "Сколько опыта нужно для уровня?",
)

SESSION_QUERIES = (
    "Сколько я уже играю?",
    "Сколько времени я играю?",
    "Как долго я уже играю?",
    "Когда я запустил игру?",
    "Покажи мою игровую сессию",
    "Покажи текущую игровую сессию",
    "Что сейчас с моей игровой сессией?",
    "Сколько сегодня я играл?",
    "Сколько часов я сегодня играл?",
    "Я играю уже сколько времени?",
)

PLAYER_STATE_QUERIES = (
    "Что у меня сейчас экипировано?",
    "Покажи мой текущий инвентарь",
    "Какой у меня сейчас loadout?",
    "Сколько у меня сейчас патронов?",
    "Какой предмет у меня в руках?",
    "Что у меня в инвентаре?",
    "Покажи мой билд",
    "Какой у меня текущий билд?",
    "Что сейчас надето на моём персонаже?",
    "Сколько у меня этого ресурса?",
)


class RoutingFirewallBenchmark(unittest.TestCase):
    def test_knowledge_never_becomes_game_session(self) -> None:
        for query in KNOWLEDGE_QUERIES:
            with self.subTest(query=query):
                self.assertFalse(is_explicit_game_session_query(query))

    def test_knowledge_never_becomes_player_state(self) -> None:
        for query in KNOWLEDGE_QUERIES:
            with self.subTest(query=query):
                self.assertFalse(is_explicit_player_state_query(query))

    def test_true_session_queries_are_still_recognized(self) -> None:
        for query in SESSION_QUERIES:
            with self.subTest(query=query):
                self.assertTrue(is_explicit_game_session_query(query))

    def test_true_player_state_queries_are_still_recognized(self) -> None:
        for query in PLAYER_STATE_QUERIES:
            with self.subTest(query=query):
                self.assertTrue(is_explicit_player_state_query(query))


if __name__ == "__main__":
    unittest.main()
