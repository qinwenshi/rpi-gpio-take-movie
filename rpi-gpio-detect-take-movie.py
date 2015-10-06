#!/usr/bin/env python

import argparse
import cv2
import time
import threading
import Queue
from cv2 import *
import RPi.GPIO as GPIO
import os
import os.path
from pushbullet import Pushbullet

# Credit: https://github.com/jrosebr1/imutils/blob/b7e36aad0e8240d5ef39a400d4df78253c1f90b8/imutils/convenience.py#L41
# Copied this function as the library doesn't work properly with an older version of OpenCV (<2.4.5)
def resize(image, width=None, height=None, inter=cv2.INTER_AREA):
    # initialize the dimensions of the image to be resized and
    # grab the image size
    dim = None
    (h, w) = image.shape[:2]

    # if both the width and height are None, then return the
    # original image
    if width is None and height is None:
        return image

    # check to see if the width is None
    if width is None:
        # calculate the ratio of the height and construct the
        # dimensions
        r = height / float(h)
        dim = (int(w * r), height)

    # otherwise, the height is None
    else:
        # calculate the ratio of the width and construct the
        # dimensions
        r = width / float(w)
        dim = (width, int(h * r))

    # resize the image
    resized = cv2.resize(image, dim, interpolation=inter)

    # return the resized image
    return resized

def on_send_to_pushbullet(api_key, video_path, msg):
    print "Initializing Pushbullet"
    pb = Pushbullet(api_key)

    print "Opening Video"
    with open(video_path, "rb") as data:
        print "Uploading Video"
        file_data = pb.upload_file(data, msg)

    print "Sending Video"
    push = pb.push_file(**file_data)

    print "Pushbullet Done!"

    print "Cleaning up old video!"
    os.remove(video_path)

current_milli_time = lambda: int(round(time.time() * 1000))

def generate_filename(extension):
    return time.strftime("%Y%m%d-%H%M%S") + "." + extension

def on_capture_frames(frame_queue, capture, video_rate):
    frame_period_secs = (float)(1.0 / video_rate)

    while True:
        pre_capture_timestamp = current_milli_time()
        if capture.isOpened():
            (grabbed, frame) = capture.read()
        else:
            raise Exception('Capture device not opened!')

        # If the frame could not be grabbed, then we have reached the end
        # of the video
        if not grabbed:
            print "Frame missing!"
            continue

        frame_queue.put(frame)

        post_capture_timestamp = current_milli_time()

        sleep_time_secs = (float)((post_capture_timestamp - pre_capture_timestamp) / 1000.0)
        if frame_period_secs > sleep_time_secs:
            sleep_time_secs = frame_period_secs - sleep_time_secs
            time.sleep(sleep_time_secs)
        else:
            print "Sleeping skipped, consider lowering framerate: " + str(frame_period_secs) + " < " + str(sleep_time_secs)


def on_run(args):
    gpio_active_state = (GPIO.HIGH if args.gpio_active == True else GPIO.LOW)
    gpio_active_px = (GPIO.PUD_DOWN if args.gpio_active == True else GPIO.PUD_UP)
    gpio_channel = args.gpio_num
    video_cap_dev = args.cap_device
    video_xres = args.xres
    video_yres = args.yres
    video_rate = args.framerate
    video_extension = "avi"
    video_format = 'XVID'
    api_key = args.api_key
    min_area = args.min_area
    detect_size = args.detect_size
    threshold = args.threshold
    blur = args.blur
    rebase_period = args.rebase_period
    pre_num_frames = video_rate * args.pre_trigger_video_length
    post_num_frames = video_rate * args.post_trigger_video_length
    debug = args.debug
    frame_stack = []

    if not os.path.exists(args.video_path):
        os.makedirs(args.video_path)
    elif not os.path.isdir(args.video_path):
        print "args.video_path must be a directory!"
        exit(1)

    print "Initializing GPIO"
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(gpio_channel, GPIO.IN, pull_up_down=gpio_active_px)

    print "Initializing OpenCV"
    base_frame = None
    detected = False
    count = 1
    capture = cv2.VideoCapture(video_cap_dev)
    format = cv2.cv.CV_FOURCC(*video_format)
    last_rebase_timestamp = time.time()

    frame_queue = Queue.Queue()
    t = threading.Thread(target=on_capture_frames, args = (frame_queue, capture, video_rate))
    t.daemon = True
    t.start()

    print "Running"
    try:
        while True:
            # Detected via gpio activation
            if not detected:
                if GPIO.input(gpio_channel) == gpio_active_state:
                    count = 1
                    detected = True
                    video_name = generate_filename(video_extension)
                    video_path = os.path.join(args.video_path, video_name)
                    video_writer = cv2.VideoWriter(video_path, format, video_rate, (video_xres, video_yres))
                    print "GPIO Activated: " + video_path

            try:
                frame = frame_queue.get(block=True, timeout=0.01)
            except:
                continue

            # Resize the frame, convert it to grayscale, and blur it
            frame = resize(frame, width=detect_size)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (blur, blur), 0)

            # Base frame
            time_diff = time.time() - last_rebase_timestamp
            if base_frame is None or (detected == False and time_diff > rebase_period):
                base_frame = gray
                last_rebase_timestamp = time.time()
                print "Getting a new base frame"
                continue

            # Compute the absolute difference between the current frame and
            # first frame
            frame_delta = cv2.absdiff(base_frame, gray)
            thresh = cv2.threshold(frame_delta, threshold, 255, cv2.THRESH_BINARY)[1]

            # Dilate the thresholded image to fill in holes, then find contours
            # on thresholded image
            thresh = cv2.dilate(thresh, None, iterations=2)
            (cnts, _) = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE)

            if debug == True:
                cv2.imshow("Security Feed", frame)
                cv2.imshow("Thresh", thresh)
                cv2.imshow("Frame Delta", frameDelta)

            # Loop over the contours
            for c in cnts:
                # If the contour is too small, ignore it
                if cv2.contourArea(c) < min_area:
                    continue

                # Compute the bounding box for the contour, draw it on the frame,
                # and update the text
                (x, y, w, h) = cv2.boundingRect(c)
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

                # Detected object on the camera
                if not detected:
                    count = 1
                    detected = True
                    video_name = generate_filename(video_extension)
                    video_path = os.path.join(args.video_path, video_name)
                    video_writer = cv2.VideoWriter(video_path, format, video_rate, (video_xres, video_yres))
                    print "Object Detected: " + video_path

            frame_stack.insert(0, frame)
            print "frame_stack length: " + str(len(frame_stack))

            # Once detected, record into a video stream and send via Pushbullet
            if detected == True:
                if len(frame_stack) > 0:
                    video_writer.write(frame_stack.pop())

                    count += 1
                    if count > (pre_num_frames + post_num_frames):
                        detected = False

                        video_writer = None

                        t = threading.Thread(target=on_send_to_pushbullet, args = (api_key, video_path, video_name))
                        t.daemon = True
                        t.start()
            elif len(frame_stack) >= pre_num_frames:
                frame_stack.pop()

    except KeyboardInterrupt:
        print "Quitting"


    print "Cleaning up GPIO's"
    GPIO.cleanup()

    print "Cleaning up OpenCV"
    capture.release()
    cv2.destroyAllWindows()

parser = argparse.ArgumentParser(description="Captures a video from a webcam when a person is detected in the camera's vision or\n"
                                             "a GPIO is triggered on the Raspberry PI.\n"
                                             "Then sends the video to the user via Pushbullet.")
parser.add_argument('-pre_trigger_video_length', help='Length of video to create in seconds before a trigger event.', type=int, default=10, required=False)
parser.add_argument('-post_trigger_video_length', help='Length of video to create in seconds after a trigger event.', type=int, default=10, required=False)
parser.add_argument('-framerate', help='Framerate to capture at.', type=int, default=20, required=False)
parser.add_argument('-xres', help='X resolution to capture at.', type=int, default=640, required=False)
parser.add_argument('-yres', help='Y resolution to capture at.', type=int, default=480, required=False)
parser.add_argument('-cap_device', help='Capture device index, if only one camera then you probably dont need to specify.', type=int, default=0, required=False)
parser.add_argument('-api_key', help='Pushbullet API key.', required=True)
parser.add_argument('-gpio_num', help='GPIO channel (BCM mode) to wait for.', type=int, required=True)
parser.add_argument('-gpio_active_high', help='Active high for when the capture should begin.', dest='gpio_active', required=False, action='store_true')
parser.add_argument('-gpio_active_low', help='Active low for when the capture should begin.', dest='gpio_active', required=False, action='store_false')
parser.add_argument('-video_path', help='Local path to save videos.', required=True)
parser.add_argument('-min_area', help='Minimum area to detect.', type=int, required=True)
parser.add_argument('-detect_size', help='Size to shrink frames down before running comparing them. This is a speed optimization.', type=int, required=True)
parser.add_argument('-threshold', help='Threshold to a person.', type=int, required=True)
parser.add_argument('-blur', help='Size of blur to apply to each frame.', type=int, required=True)
parser.add_argument('-rebase_period', help='Frequency in seconds to update our base frame (providing the system isnt in the middle of a detection).', type=int, required=True)
parser.add_argument('-debug', help='Enable debugging.', required=False, action='store_true')
parser.set_defaults(gpio_active=False)
parser.set_defaults(func=on_run)
args = parser.parse_args()
args.func(args)
