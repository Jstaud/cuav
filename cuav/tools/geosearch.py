#!/usr/bin/python

import numpy, os, time, cv, sys, math, sys, glob
import multiprocessing

from cuav.lib import cuav_util
from cuav.image import scanner
from cuav.lib import cuav_mosaic, mav_position, cuav_joe, cuav_region
from cuav.camera import cam_params
from MAVProxy.modules.mavproxy_map import mp_slipmap
from MAVProxy.modules.mavproxy_map import mp_image

slipmap = None
mosaic = None

def file_list(directory, extensions):
  '''return file list for a directory'''
  flist = []
  for (root, dirs, files) in os.walk(directory):
    for f in files:
      extension = f.split('.')[-1]
      if extension.lower() in extensions:
        flist.append(os.path.join(root, f))
  return flist

def process(args):
  '''process a set of files'''

  global slipmap, mosaic
  scan_count = 0
  files = []
  for a in args:
    if os.path.isdir(a):
      files.extend(file_list(a, ['jpg', 'pgm', 'png']))
    else:
      if a.find('*') != -1:
        files.extend(glob.glob(a))
      else:
        files.append(a)
  files.sort()
  num_files = len(files)
  print("num_files=%u" % num_files)
  region_count = 0

  slipmap = mp_slipmap.MPSlipMap(service=opts.service, elevation=True, title='Map')
  icon = slipmap.icon('redplane.png')
  slipmap.add_object(mp_slipmap.SlipIcon('plane', (0,0), icon, layer=3, rotation=0,
                                         follow=True,
                                         trail=mp_slipmap.SlipTrail()))

  if opts.mission:
    from pymavlink import mavwp
    wp = mavwp.MAVWPLoader()
    wp.load(opts.mission)
    boundary = wp.polygon()
    slipmap.add_object(mp_slipmap.SlipPolygon('mission', boundary, layer=1,
                                              linewidth=1, colour=(255,255,255)))


  if opts.mavlog:
    mpos = mav_position.MavInterpolator()
    mpos.set_logfile(opts.mavlog)
  else:
    mpos = None

  if opts.kmzlog:
    kmzpos = mav_position.KmlPosition(opts.kmzlog)
  else:
    kmzpos = None

  if opts.triggerlog:
    triggerpos = mav_position.TriggerPosition(opts.triggerlog)
  else:
    triggerpos = None

  # create a simple lens model using the focal length
  C_params = cam_params.CameraParams(lens=opts.lens, sensorwidth=opts.sensorwidth)

  if opts.camera_params:
    C_params.load(opts.camera_params)

  mosaic = cuav_mosaic.Mosaic(slipmap, C=C_params)

  joelog = cuav_joe.JoeLog(None)

  if opts.view:
    viewer = mp_image.MPImage(title='Image', can_zoom=True, can_drag=True)

  scan_parms = {
    'MinRegionArea' : opts.min_region_area,
    'MaxRegionArea' : opts.max_region_area,
    'MinRegionSize' : opts.min_region_size,
    'MaxRegionSize' : opts.max_region_size,
    'MaxRarityPct'  : opts.max_rarity_pct,
    'RegionMergeSize' : opts.region_merge
    }

  for f in files:
      if mpos:
        # get the position by interpolating telemetry data from the MAVLink log file
        # this assumes that the filename contains the timestamp 
        frame_time = cuav_util.parse_frame_time(f) + opts.time_offset
        if opts.roll_stabilised:
          roll = 0
        else:
          roll = None
        try:
          pos = mpos.position(frame_time, roll=roll)
        except Exception:
          print("No position available for %s" % frame_time)
          # skip this frame
          continue
      elif kmzpos is not None:
        pos = kmzpos.position(f)
      elif triggerpos is not None:
        pos = triggerpos.position(f)
      else:
        # get the position using EXIF data
        pos = mav_position.exif_position(f)
        pos.time += opts.time_offset

      # update the plane icon on the map
      if pos is not None:
        slipmap.set_position('plane', (pos.lat, pos.lon), rotation=pos.yaw)

      # check for any events from the map
      slipmap.check_events()
      mosaic.check_events()

      im_orig = cuav_util.LoadImage(f)
      (w,h) = cuav_util.image_shape(im_orig)

      if not opts.camera_params:
        C_params.set_resolution(w, h)
      
      im_full = im_orig
        
      im_640 = cv.CreateImage((640, 480), 8, 3)
      cv.Resize(im_full, im_640, cv.CV_INTER_NN)
      im_640 = numpy.ascontiguousarray(cv.GetMat(im_640))
      im_full = numpy.ascontiguousarray(cv.GetMat(im_full))

      count = 0
      total_time = 0

      t0=time.time()
      if opts.fullres:
        img_scan = im_full
      else:
        img_scan = im_640

      if pos is not None:
        (sw,sh) = cuav_util.image_shape(img_scan)
        mpp = cuav_util.meters_per_pixel(pos, C=C_params)
        if mpp is not None:
          scan_parms['MetersPerPixel'] = mpp * (w/float(sw))
        regions = scanner.scan(img_scan, scan_parms)
      else:
        regions = scanner.scan(img_scan)
      regions = cuav_region.RegionsConvert(regions, cuav_util.image_shape(img_scan), cuav_util.image_shape(im_full))
      count += 1
      t1=time.time()

      frame_time = pos.time

      regions = cuav_region.filter_regions(im_full, regions, frame_time=frame_time,
                                           min_score=opts.minscore, filter_type=opts.filter_type)

      scan_count += 1

      mosaic.add_image(pos.time, f, pos)

      if pos and len(regions) > 0:
        joelog.add_regions(frame_time, regions, pos, f, width=w, height=h, altitude=opts.altitude)

      region_count += len(regions)

      if len(regions) > 0:
          composite = cuav_mosaic.CompositeThumbnail(cv.GetImage(cv.fromarray(im_full)), regions)
          thumbs = cuav_mosaic.ExtractThumbs(composite, len(regions))
          mosaic.add_regions(regions, thumbs, f, pos)

      if opts.view:
        img_view = img_scan
        (wview,hview) = cuav_util.image_shape(img_view)
        mat = cv.fromarray(img_view)
        for r in regions:
          r.draw_rectangle(mat, (255,0,0))
        cv.CvtColor(mat, mat, cv.CV_BGR2RGB)
        viewer.set_image(mat)
        viewer.set_title('Image: ' + os.path.basename(f))

      total_time += (t1-t0)
      if t1 != t0:
          print('%s scan %.1f fps  %u regions [%u/%u]' % (
              os.path.basename(f), count/total_time, region_count, scan_count, num_files))
      #raw_input("hit ENTER when ready")


def parse_args():
  '''parse command line arguments'''
  if 1 == len(sys.argv):
    from MAVProxy.modules.lib.optparse_gui import OptionParser
    file_type='file'
    directory_type='directory'
  else:
    from optparse import OptionParser
    file_type='str'
    directory_type='str'

  parser = OptionParser("geosearch.py [options] <directory>", description='GeoSearch')

  parser.add_option("--directory", default=None, type=directory_type,
                    help="directory containing image files")
  parser.add_option("--mission", default=None, type=file_type, help="mission file to display")
  parser.add_option("--mavlog", default=None, type=file_type, help="MAVLink telemetry log file")
  parser.add_option("--kmzlog", default=None, type=file_type, help="kmz file for image positions")
  parser.add_option("--triggerlog", default=None, type=file_type, help="robota trigger file for image positions")
  parser.add_option("--minscore", default=500, type='int', help="minimum score")
  parser.add_option("--filter-type", type='choice', default='simple', choices=['simple', 'compactness'], help="object filter type")
  parser.add_option("--time-offset", type='int', default=0, help="offset between camera and mavlink log times (seconds)")
  parser.add_option("--altitude", type='int', default=90, help="camera altitude above ground (meters)")
  parser.add_option("--view", action='store_true', default=False, help="show images")
  parser.add_option("--lens", default=28.0, type='float', help="lens focal length")
  parser.add_option("--sensorwidth", default=35.0, type='float', help="sensor width")
  parser.add_option("--service", default='MicrosoftSat', help="map service")
  parser.add_option("--camera-params", default=None, type=file_type, help="camera calibration json file from OpenCV")
  parser.add_option("--roll-stabilised", default=False, action='store_true', help="roll is stabilised")
  parser.add_option("--fullres", action='store_true', default=False, help="scan at full resolution")
  parser.add_option("--min-region-area", default=0.003, type='float', help="minimum region area (m^2)")
  parser.add_option("--max-region-area", default=2.0, type='float', help="maximum region area (m^2)")
  parser.add_option("--min-region-size", default=0.05, type='float', help="minimum region size (m)")
  parser.add_option("--max-region-size", default=2.0, type='float', help="maximum region size (m)")
  parser.add_option("--region-merge", default=0.5, type='float', help="region merge size (m)")
  parser.add_option("--max-rarity-pct", default=0.02, type='float', help="maximum percentage rarity (percent)")
  return parser.parse_args()

if __name__ == '__main__':
  multiprocessing.freeze_support()
  (opts, args) = parse_args()

  # main program
  if opts.directory is not None:
    process([opts.directory])
  else:
    process(args)
  while True:
    slipmap.check_events()
    mosaic.check_events()
    time.sleep(0.002)
