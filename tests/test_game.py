"""Rules of the grid, no GTK needed:  python3 -m unittest discover -s tests"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["XDG_DATA_HOME"] = "/tmp/phosphor-snake-test-xdg"       # never touch the real high score
import phosphor_snake as ps


class GameRules(unittest.TestCase):
    def game(self, **kw):
        g = ps.Game(cols=12, rows=8, seed=1, **kw); g.reset(); g.state = "playing"; return g

    def test_moves_as_a_solid_body_with_no_trail(self):
        g = self.game(); head = g.snake[0]; tail = g.snake[-1]
        g.step()
        self.assertEqual(g.snake[0], (head[0] + 1, head[1]))
        self.assertNotIn(tail, g.snake); self.assertEqual(g.embers, {})    # the vacated cell is simply empty
        self.assertEqual(g.length, 4)

    def test_glide_progress_and_turns_latch_at_cell_boundaries(self):
        g = self.game(); g.acc = 0
        g.tick(g.step_ms / 1000 * 0.5)
        self.assertAlmostEqual(g.progress, 0.5, places=2)                  # half-way into the next cell
        g.turn("up")
        self.assertEqual(g.direction, "right")                               # the glide in flight keeps its heading
        g.tick(g.step_ms / 1000 * 0.5 + 1e-4)
        self.assertEqual(g.direction, "up")                                 # ...and the turn applies from the boundary
        self.assertLess(g.progress, 0.05)
        hx, hy = g.snake[0]; g.food = (hx, hy - 1)
        self.assertTrue(g.will_grow())

    def test_eats_grows_scores_and_speeds_up(self):
        g = self.game(); hx, hy = g.snake[0]; g.food = (hx + 1, hy); ms0 = g.step_ms
        g.step()
        self.assertEqual(g.length, 5); self.assertEqual(g.eaten, 1); self.assertGreater(g.score, 0)
        self.assertLess(g.step_ms, ms0); self.assertNotEqual(g.food, (hx + 1, hy)); self.assertGreater(g.hit, 0.9)
        self.assertTrue(any("TARGET" in m for _, m in g.log))

    def test_wall_kills_unless_wrap(self):
        g = self.game(); g.snake.clear(); g.snake.extend([(11, 4), (10, 4), (9, 4)]); g.direction = "right"
        g.step(); self.assertEqual(g.state, "over")
        w = self.game(wrap=True); w.snake.clear(); w.snake.extend([(11, 4), (10, 4), (9, 4)]); w.direction = "right"
        w.step(); self.assertEqual(w.state, "playing"); self.assertEqual(w.snake[0], (0, 4))

    def test_self_collision_but_not_with_the_moving_tail(self):
        g = self.game()
        g.snake.clear(); g.snake.extend([(5, 5), (5, 4), (4, 4), (4, 5), (4, 6)]); g.direction = "left"
        g.step(); self.assertEqual(g.state, "over")                       # (4,5) is body
        h = self.game()
        h.snake.clear(); h.snake.extend([(5, 5), (5, 4), (4, 4), (4, 5)]); h.direction = "left"
        h.step(); self.assertEqual(h.state, "playing"); self.assertEqual(h.snake[0], (4, 5))   # tail vacated it

    def test_turn_queue_rejects_reversal_and_caps_at_two(self):
        g = self.game()                                   # heading right
        g.turn("left"); self.assertEqual(list(g.queue), [])
        g.turn("up"); g.turn("down"); self.assertEqual(list(g.queue), ["up"])   # down reverses the queued up
        g.turn("left"); g.turn("down"); self.assertEqual(list(g.queue), ["up", "left"])
        g.step(); self.assertEqual(g.direction, "up")

    def test_tick_runs_steps_by_time_and_spectrum_tracks_body(self):
        g = self.game(); g.acc = 0
        g.tick(g.step_ms / 1000 * 2.5)
        self.assertEqual(g.snake[0][0], 8)                                  # 6 + 2 steps
        for _ in range(30): g.tick(0.05)
        cols = {x for x, _ in g.snake}
        self.assertTrue(all(g.level[x] > 0.05 for x in cols))
        self.assertTrue(all(g.peak[x] >= g.level[x] for x in range(g.cols)))

    def test_toggle_state_machine(self):
        g = ps.Game(cols=12, rows=8, seed=1)
        self.assertEqual(g.state, "attract"); g.toggle(); self.assertEqual(g.state, "playing")
        g.toggle(); self.assertEqual(g.state, "paused"); g.toggle(); self.assertEqual(g.state, "playing")
        g._die("TEST"); g.toggle(); self.assertEqual(g.state, "playing"); self.assertEqual(g.score, 0)

    def test_renderer_draws_every_state(self):
        r = ps.Renderer(); r.resize(640, 400)
        for st in ("attract", "playing", "paused", "over"):
            g = ps.Game(cols=12, rows=8, seed=1); g.reset(); g.state = st
            surf = r.render(g); self.assertEqual(surf.get_width(), 640)


if __name__ == "__main__":
    unittest.main()
