import gamera.core as gc
gc.init_gamera()
import matplotlib.pyplot as plt
from gamera.plugins.image_utilities import union_images
import textAlignPreprocessing as preproc
import os
import PIL
import numpy as np
import textSeqCompare as tsc
import latinSyllabification as latsyl
import subprocess
from PIL import Image, ImageDraw, ImageFont
reload(preproc)
reload(tsc)
reload(latsyl)

filename = 'salzinnes_18'
ocropus_model = './ocropy-master/models/salzinnes_model-00054500.pyrnn.gz'
parallel = 2
median_line_mult = 2


# removes some special characters from OCR output. ideally these would be useful but not clear how
# best to integrate them into the alignment algorithm. unidecode doesn't seem to work with these
# either
def clean_special_chars(inp):
    inp = inp.replace('~', '')
    inp = inp.replace('\xc4\x81', 'a')
    inp = inp.replace('\xc4\x93', 'e')
    # there is no i with bar above in unicode (???)
    inp = inp.replace('\xc5\x8d', 'o')
    inp = inp.replace('\xc5\xab', 'u')
    return inp

#######################
# -- PRE-PROCESSING --
#######################

# get raw image of text layer and preform preprocessing to find text lines
raw_image = gc.load_image('./png/' + filename + '_text.png')
image, staff_image = preproc.preprocess_images(raw_image, None)
cc_lines, lines_peak_locs = preproc.identify_text_lines(image)

# get bounding box around each line, with padding (does padding affect ocropus output?)
cc_strips = []
for line in cc_lines:
    pad = 0
    x_min = min(c.offset_x for c in line) - pad
    x_max = max(c.offset_x + c.width for c in line) + pad
    y_max = max(c.offset_y + c.height for c in line) + pad

    # we want to cut off the tops of large capital letters, because that's how the model was
    # trained. set the top to be related to the median rather than the minimum y-coordinate
    y_min = min(c.offset_y for c in line)
    y_med_height = np.median([c.height for c in line]) * median_line_mult
    y_min = max(y_max - y_med_height, y_min)

    cc_strips.append(image.subimage((x_min, y_min), (x_max, y_max)))

# make directory to do stuff in
dir = 'wkdir_' + filename
if not os.path.exists(dir):
    subprocess.check_call("mkdir " + dir, shell=True)

# save strips to directory
for i, strip in enumerate(cc_strips):
    strip.save_image('./{}/{}_{}.png'.format(dir, filename, i))

#################################
# -- PERFORM OCR WITH OCROPUS --
#################################

# call ocropus command to do OCR on each saved line strip
ocropus_command = 'ocropus-rpred -Q {} --nocheck --llocs -m {} \'{}/*.png\''.format(parallel, ocropus_model, dir)
subprocess.check_call(ocropus_command, shell=True)

# read character position results from llocs file
all_chars = []
other_chars = []
for i in range(len(cc_strips)):
    locs_file = './{}/{}_{}.llocs'.format(dir, filename, i)
    with open(locs_file) as f:
        locs = [line.rstrip('\n') for line in f]

    x_min = cc_strips[i].offset_x
    y_min = cc_strips[i].offset_y
    y_max = cc_strips[i].offset_y + cc_strips[i].height

    # note: ocropus seems to associate every character with its RIGHTMOST edge. we want the
    # left-most edge, so we associate each character with the previous char's right edge
    text_line = []
    prev_xpos = x_min
    for l in locs:
        lsp = l.split('\t')
        cur_xpos = float(lsp[1]) + x_min

        ul = (prev_xpos, y_min)
        lr = (cur_xpos, y_max)

        if lsp[0] == '~' or lsp[0] == '':
            other_chars.append((lsp[0], ul, lr))
        else:
            all_chars.append((clean_special_chars(lsp[0]), ul, lr))

        prev_xpos = cur_xpos

# delete working directory
subprocess.check_call("rm -r " + dir, shell=True)

# get full ocr transcript
ocr = ''.join(x[0] for x in all_chars)
all_chars_copy = list(all_chars)

###################################
# -- PERFORM AND PARSE ALIGNMENT --
###################################

transcript = tsc.read_file('./png/' + filename + '_transcript.txt')
tra_align, ocr_align = tsc.process(transcript, ocr)

align_transcript_chars = []

# insert gaps into ocr output based on alignment string. this causes all_chars to have gaps at the
# same points as the ocr_align string does, and is thus the same length as tra_align.
for i, char in enumerate(ocr_align):
    if char == '_':
        all_chars.insert(i, ('_', 0, 0))

# this could very possibly go wrong (special chars, bug in alignment algorithm, etc) so better
# make sure that this condition is holding at this point
assert len(all_chars) == len(tra_align), 'all_chars not same length as alignment'

for i, ocr_char in enumerate(all_chars):
    tra_char = tra_align[i]

    if not (tra_char == '_' or ocr_char[0] == '_'):
        align_transcript_chars.append([tra_char, ocr_char[1], ocr_char[2]])
    elif (tra_char != '_'):
        align_transcript_chars[-1][0] += tra_char

#############################
# -- GROUP INTO SYLLABLES --
#############################

syls = latsyl.syllabify_text(transcript)
syls_boxes = []

# get bounding boxes for each syllable
syl_pos = -1                        # track of which syllable trying to get box of
char_accumulator = ''               # check cur syl against this
get_new_syl = True                  # flag that next loop should start a new syllable
cur_ul = 0                          # upper-left point of last unassigned character
for c in align_transcript_chars:    # @char can have more than one char in char[0]. yeah, i know.

    char_text = c[0].replace(' ', '')
    if not char_text:
        continue

    if get_new_syl:
        get_new_syl = False
        syl_pos += 1
        cur_syl = syls[syl_pos]
        cur_ul = c[1]

    cur_lr = c[2]
    char_accumulator += char_text
    print (cur_syl, char_accumulator, cur_ul, cur_lr)
    # if the accumulator has got the current syllable in it, remove the current syllable
    # from the accumulator and assign that syllable to the bounding box between cur_ul and cur_lr.
    # note that a syllable can be 'split,' in which case char_accumulator will have chars left in it
    if cur_syl in char_accumulator:
        char_accumulator = char_accumulator[len(cur_syl):]
        syls_boxes.append((cur_syl, cur_ul, cur_lr))
        get_new_syl = True

#############################
# -- DRAW RESULTS ON PAGE --
#############################

im = image.to_greyscale().to_pil()
text_size = 80
fnt = ImageFont.truetype('Arial.ttf', text_size)
draw = ImageDraw.Draw(im)

for i, char in enumerate(syls_boxes):
    if char[0] in '. ':
        continue

    ul = char[1]
    lr = char[2]
    draw.text((ul[0], ul[1] - text_size), char[0], font=fnt, fill='gray')
    draw.rectangle([ul, lr], outline='black')
    draw.line([ul[0], ul[1], ul[0], lr[1]], fill='black', width=10)

for i, peak_loc in enumerate(lines_peak_locs):
    draw.text((1, peak_loc - text_size), 'line {}'.format(i), font=fnt, fill='gray')
    draw.line([0, peak_loc, im.width, peak_loc], fill='gray', width=3)

im.save('testimg_{}.png'.format(filename))
im.show()
