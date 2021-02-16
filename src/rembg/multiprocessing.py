import math
import multiprocessing
import re
import subprocess as sp
import time

import ffmpeg
import moviepy.editor as mpy
from more_itertools import chunked

from .bg import remove_many


def worker(worker_nodes,
           worker_index,
           result_dict,
           model_name,
           gpu_batchsize,
           total_frames,
           frames_dict):
    print(F"WORKER {worker_index} ONLINE")

    frame_indexes = chunked(range(total_frames), gpu_batchsize)[worker_index::worker_nodes - 1]
    worker_index += 1

    while True:
        fi = list(next(frame_indexes, []))
        if not fi:
            break

        # are we processing frames faster than the frame ripper is saving them?
        last = fi[-1]
        while last not in frames_dict:
            time.sleep(0.1)

        result_dict[worker_index] = remove_many([frames_dict[index] for index in fi], model_name)

        # clean up the frame buffer
        for fdex in fi:
            del frames_dict[fdex]
        worker_index += worker_nodes


def capture_frames(file_path, frames_dict):
    print(F"WORKER FRAMERIPPER ONLINE")

    for frame in mpy.VideoFileClip(file_path).resize(height=320).iter_frames(dtype="uint8"):
        frames_dict[frame[0]] = frame[1]


def parallel_greenscreen(file_path,
                         worker_nodes,
                         gpu_batchsize,
                         model_name,
                         frame_limit):
    manager = multiprocessing.Manager()

    results_dict = manager.dict()
    frames_dict = manager.dict()

    info = ffmpeg.probe(file_path)
    total_frames = int(info["streams"][0]["nb_frames"])

    if frame_limit != -1:
        total_frames = min(frame_limit, total_frames)

    frame_rate = math.ceil(eval(info["streams"][0]["r_frame_rate"]))

    print(F"FRAME RATE: {frame_rate} TOTAL FRAMES: {total_frames}")

    p = multiprocessing.Process(target=capture_frames, args=(file_path, frames_dict))
    p.start()

    # note I am deliberatley not using pool
    # we can't trust it to run all the threads concurrently (or at all)
    workers = [multiprocessing.Process(target=worker,
                                       args=(worker_nodes, wn, results_dict, model_name, gpu_batchsize, total_frames,
                                             frames_dict))
               for wn in range(worker_nodes)]
    for w in workers:
        w.start()

    command = None
    proc = None
    frame_counter = 0

    for i in range(math.ceil(total_frames / worker_nodes)):
        for wx in range(worker_nodes):

            hash_index = i * worker_nodes + 1 + wx

            while hash_index not in results_dict:
                time.sleep(0.1)

            frames = results_dict[hash_index]
            # dont block access to it anymore
            del results_dict[hash_index]

            for frame in frames:
                if command is None:
                    command = ['FFMPEG',
                               '-y',
                               '-f', 'rawvideo',
                               '-vcodec', 'rawvideo',
                               '-s', F"{frame.shape[1]}x320",
                               '-pix_fmt', 'gray',
                               '-r', F"{frame_rate}",
                               '-i', '-',
                               '-an',
                               '-vcodec', 'mpeg4',
                               '-b:v', '2000k',
                               re.sub("\.(mp4|mov|avi)", ".matte.\\1", file_path, flags=re.I)]

                    proc = sp.Popen(command, stdin=sp.PIPE)

                proc.stdin.write(frame.tostring())
                frame_counter = frame_counter + 1

                if frame_counter >= total_frames:
                    proc.stdin.close()
                    proc.wait()
                    print(F"FINISHED ALL FRAMES ({total_frames})!")
                    return

    p.join()
    for w in workers:
        w.join()

    proc.stdin.close()
    proc.wait()
