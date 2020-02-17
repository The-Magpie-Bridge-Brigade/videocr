from __future__ import annotations
from typing import List
import sys
import multiprocessing
import pytesseract
import cv2
import pathlib

from . import constants
from . import utils
from .models import PredictedFrame, PredictedSubtitle
from .opencv_adapter import Capture


class Video:
    path: str
    lang: str
    use_fullframe: bool
    width_boundary: tuple
    height_boundary: tuple
    num_frames: int
    fps: float
    width: int
    height: int
    pred_frames: List[PredictedFrame]
    pred_subs: List[PredictedSubtitle]

    def __init__(self, path: str):
        self.path = path
        with Capture(path) as v:
            self.num_frames = int(v.get(cv2.CAP_PROP_FRAME_COUNT))
            self.fps = v.get(cv2.CAP_PROP_FPS)
            self.width = int(v.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.height = int(v.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def run_ocr(self, lang: str, time_start: str, time_end: str,
                conf_threshold: int, use_fullframe: bool,
                width_boundary: tuple, height_boundary: tuple, img_path: str) -> None:
        self.lang = lang
        self.use_fullframe = use_fullframe
        self.width_boundary = width_boundary
        self.height_boundary = height_boundary

        ocr_start = utils.get_frame_index(time_start, self.fps) if time_start else 0
        ocr_end = utils.get_frame_index(time_end, self.fps) if time_end else self.num_frames

        if ocr_end < ocr_start:
            raise ValueError('time_start is later than time_end')
        # num_ocr_frames = ocr_end - ocr_start
        num_ocr_frames = 1

        # get frames from ocr_start to ocr_end
        with Capture(self.path) as v, multiprocessing.Pool() as pool:
            v.set(cv2.CAP_PROP_POS_FRAMES, ocr_start)
            frames = (v.read()[1] for _ in range(num_ocr_frames))
            
            for i, f in enumerate(frames):
                p = pathlib.Path(img_path, '{}.png'.format(i)).resolve()
                p = str(p)
                print("img_path: {}".format(p))
                f = self._crop(f, self.width, self.height, use_fullframe, width_boundary, height_boundary)
                #f = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                #_, f = cv2.threshold(f, 220, 255, cv2.THRESH_BINARY)
                cv2.imwrite(p, f)

                config = '--tessdata-dir "{}"'.format(constants.TESSDATA_DIR)
                try:
                    sub = pytesseract.image_to_data(f, lang=self.lang, config=config)
                except Exception as e:
                    sys.exit('{}: {}'.format(e.__class__.__name__, e))
                print(sub)
                predicted = PredictedFrame(0, sub, 80)
                print(predicted.text)

            return
            # perform ocr to frames in parallel
            it_ocr = pool.imap(self._image_to_data, frames, chunksize=10)
            self.pred_frames = [
                PredictedFrame(i + ocr_start, data, conf_threshold)
                for i, data in enumerate(it_ocr)
            ]

    def _crop(self, img, width: int, height: int, use_fullframe:bool, width_boundary: tuple, height_boundary: tuple):
        if width_boundary or height_boundary:
            xa = 0
            xb = width
            ya = 0
            yb = height

            if width_boundary:
                xa, xb = width_boundary
            if height_boundary:
                ya, yb = height_boundary
            
            img = img[ya:yb, xa:xb]
        elif not use_fullframe:
            # only use bottom half of the frame by default
            img = img[height // 2:, :]
        
        return img
    
    def _filter(self, img):
        pass

    def _image_to_data(self, img) -> str:
        if self.width_boundary or self.height_boundary:
            xa = 0
            xb = self.width - 1
            ya = 0
            yb = self.height - 1

            if self.width_boundary:
                xa, xb = self.width_boundary
            if self.height_boundary:
                ya, yb = self.height_boundary
            
            img = img[ya:yb, xa:xb]
        elif not self.use_fullframe:
            # only use bottom half of the frame by default
            img = img[self.height // 2:, :]
        config = '--tessdata-dir "{}"'.format(constants.TESSDATA_DIR)
        try:
            return pytesseract.image_to_data(img, lang=self.lang, config=config)
        except Exception as e:
            sys.exit('{}: {}'.format(e.__class__.__name__, e))

    def get_subtitles(self, sim_threshold: int) -> str:
        self._generate_subtitles(sim_threshold)
        return ''.join(
            '{}\n{} --> {}\n{}\n\n'.format(
                i,
                utils.get_srt_timestamp(sub.index_start, self.fps),
                utils.get_srt_timestamp(sub.index_end, self.fps),
                sub.text)
            for i, sub in enumerate(self.pred_subs))

    def _generate_subtitles(self, sim_threshold: int) -> None:
        self.pred_subs = []

        if self.pred_frames is None:
            raise AttributeError(
                'Please call self.run_ocr() first to perform ocr on frames')

        # divide ocr of frames into subtitle paragraphs using sliding window
        WIN_BOUND = int(self.fps // 2)  # 1/2 sec sliding window boundary
        bound = WIN_BOUND
        i = 0
        j = 1
        while j < len(self.pred_frames):
            fi, fj = self.pred_frames[i], self.pred_frames[j]

            if fi.is_similar_to(fj):
                bound = WIN_BOUND
            elif bound > 0:
                bound -= 1
            else:
                # divide subtitle paragraphs
                para_new = j - WIN_BOUND
                self._append_sub(PredictedSubtitle(
                    self.pred_frames[i:para_new], sim_threshold))
                i = para_new
                j = i
                bound = WIN_BOUND

            j += 1

        # also handle the last remaining frames
        if i < len(self.pred_frames) - 1:
            self._append_sub(PredictedSubtitle(
                self.pred_frames[i:], sim_threshold))

    def _append_sub(self, sub: PredictedSubtitle) -> None:
        if len(sub.text) == 0:
            return

        # merge new sub to the last subs if they are similar
        while self.pred_subs and sub.is_similar_to(self.pred_subs[-1]):
            ls = self.pred_subs[-1]
            del self.pred_subs[-1]
            sub = PredictedSubtitle(ls.frames + sub.frames, sub.sim_threshold)

        self.pred_subs.append(sub)
