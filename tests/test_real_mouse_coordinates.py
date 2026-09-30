import unittest
from unittest.mock import Mock, patch

from common.services.captcha import real_mouse_coordinates as coords


class SliderCalibrationTests(unittest.TestCase):
    def calibrate(self, observations, mapper=None):
        mapper = mapper or coords.ScreenMapper(0, 0, 1.25)
        button = Mock()
        button.bounding_box.return_value = dict(x=80, y=180, width=40, height=40)
        button.evaluate.return_value = [50, 60]
        with (
            patch.object(coords, '_glide_move_abs'),
            patch.object(coords, '_clear_events'),
            patch.object(coords, '_read_observation', side_effect=observations),
            patch.object(coords, 'virtual_screen', return_value=(0, 0, 2560, 1440)),
            patch.object(coords.time, 'sleep'),
        ):
            result = coords.calibrate_slider_center(Mock(), Mock(), button, mapper)
        return mapper, result

    def test_corrects_upward_offset_in_iframe_at_125_percent(self):
        mapper, (ok, diag) = self.calibrate([
            ((50, 20, 0, 0), (50, 60), 'slider_frame'),
            ((50, 60, 0, 0), (50, 60), 'slider_frame'),
        ])
        self.assertTrue(ok)
        self.assertEqual(mapper.correction_y, 50)
        self.assertEqual(diag['corrected_screen'], (125, 300))

    def test_main_frame_uses_viewport_not_iframe_coordinates(self):
        _, (ok, diag) = self.calibrate([
            ((90, 160, 0, 0), (100, 200), 'main_frame'),
            ((50, 60, 0, 0), (50, 60), 'slider_frame'),
        ])
        self.assertTrue(ok)
        self.assertEqual(diag['correction_physical'], (12.5, 50))

    def test_missing_fresh_event_cannot_reuse_previous_success(self):
        _, (ok, diag) = self.calibrate([
            ((50, 60, 0, 0), (50, 60), 'slider_frame'),
            ((), (100, 200), 'none'),
        ])
        self.assertFalse(ok)
        self.assertEqual(diag['error'], 'no_fresh_mouse_event_after_correction')

    def test_inaccurate_correction_fails_closed(self):
        _, (ok, _) = self.calibrate([
            ((50, 20, 0, 0), (50, 60), 'slider_frame'),
            ((50, 56, 0, 0), (50, 60), 'slider_frame'),
        ])
        self.assertFalse(ok)

    def test_no_real_input_fails_closed(self):
        _, (ok, diag) = self.calibrate([((), (100, 200), 'none')] * 13)
        self.assertFalse(ok)
        self.assertEqual(diag['error'], 'page_received_no_real_mouse_event')


if __name__ == '__main__':
    unittest.main()
