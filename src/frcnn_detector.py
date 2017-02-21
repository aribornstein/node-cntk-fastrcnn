import sys
import cv2
import numpy as np
from os import path
from cntk import load_model
from time import time
import json

# constants used for ROI generation:
# ROI generation
# TODO: Read those values from PARAMETERS.py?
roi_minDimRel = 0.04
roi_maxDimRel = 0.4
roi_minNrPixelsRel = 2 * roi_minDimRel * roi_minDimRel
roi_maxNrPixelsRel = 0.33 * roi_maxDimRel * roi_maxDimRel
roi_maxAspectRatio = 4.0  # maximum aspect Ratio of a ROI vertically and horizontally
roi_maxImgDim = 200  # image size used for ROI generation
ss_scale = 100  # selective search ROIS: parameter controlling cluster size for segmentation
ss_sigma = 1.2  # selective search ROIs: width of gaussian kernal for segmentation
ss_minSize = 20  # selective search ROIs: minimum component size for segmentation
grid_nrScales = 7  # uniform grid ROIs: number of iterations from largest possible ROI to smaller ROIs
grid_aspectRatios = [1.0, 2.0, 0.5]  # uniform grid ROIs: aspect ratio of ROIs
#cntk_nrRois = 2000  # 2000 # how many ROIs to zero-pad
#cntk_padWidth = 1000
#cntk_padHeight = 1000
roi_minDim = roi_minDimRel * roi_maxImgDim
roi_maxDim = roi_maxDimRel * roi_maxImgDim
roi_minNrPixels = roi_minNrPixelsRel * roi_maxImgDim * roi_maxImgDim
roi_maxNrPixels = roi_maxNrPixelsRel * roi_maxImgDim * roi_maxImgDim
nms_threshold = 0.1


def get_classes_description(model_file_path, classes_count):
    model_dir = path.dirname(model_file_path)
    classes_names = {}
    model_desc_file_path = path.join(model_dir, 'model.json')
    if not path.exists(model_desc_file_path):
        # use default parameter names:
        for i in range(classes_count):
            classes_names["class_%d"%i] = i
        return classes_names
    
    with open(model_desc_file_path) as handle:
        file_content = handle.read()
        model_desc = json.loads(file_content)
    return model_desc["classes"]
   

class StopWatch:
    def __init__(self, silent=False):
        self.__silent = silent
        self.time = time()

    def t(self, message):
        if self.__silent:
            return

        print("watch: %s: %0.4f" % (message, time() - self.time))
        self.time = time()

class FRCNNDetector:

    def __init__(self, model_path,
                 pad_value = 114, cntk_scripts_path=r"c:\local\cntk\Examples\Image\Detection\FastRCNN",
                 use_selective_search_rois = True,
                 use_grid_rois = True):
        self.__model_path = model_path
        self.__cntk_scripts_path = cntk_scripts_path
        self.__pad_value = pad_value
        self.__pad_value_rgb = [pad_value, pad_value, pad_value]
        self.__use_selective_search_rois = use_selective_search_rois
        self.__use_grid_rois = use_grid_rois
        self.__model = None
        self.__model_warm = False
        self.__grid_rois_cache = {}
        self.rois_predictions_labels = None
        self.labels_count = 0

        # a cache to use ROIs after filter in case we only use the grid method
        self.__rois_only_grid_cache = {}

        # A really horrible hack we do for now in order to take the scripts from the CNTK exampels dir..
        # Ideally we should just take the entire dir
        sys.path.append(self.__cntk_scripts_path)
        global imArrayWidthHeight, getSelectiveSearchRois, imresizeMaxDim
        from cntk_helpers import imArrayWidthHeight, getSelectiveSearchRois, imresizeMaxDim
        global getGridRois, filterRois, roiTransformPadScaleParams, roiTransformPadScale
        from cntk_helpers import getGridRois, filterRois, roiTransformPadScaleParams, roiTransformPadScale
        global softmax2D, applyNonMaximaSuppression
        from cntk_helpers import softmax2D, applyNonMaximaSuppression

    def ensure_model_is_loaded(self):
        if not self.__model:
            self.load_model()

    def warm_up(self):
        self.ensure_model_is_loaded()

        if self.__model_warm:
            return

        # a dummy variable for labels the will be given as an input to the network but will be ignored
        dummy_labels = np.zeros((self.__nr_rois, self.labels_count))
        dummy_rois = np.zeros((self.__nr_rois, 4))
        dummy_image = np.ones((3, self.__resize_width, self.__resize_height)) * 255.0

        # prepare the arguments
        arguments = {
            self.__model.arguments[self.__args_indices["features"]]: [dummy_image],
            self.__model.arguments[self.__args_indices["rois"]]: [dummy_rois],
            self.__model.arguments[self.__args_indices["roiLabels"]]: [dummy_labels]
        }
        self.__model.eval(arguments)

        self.__model_warm = True


    def load_model(self):
        if self.__model:
            raise Exception("Model already loaded")
        self.__model = load_model(self.__model_path)
        self.__args_indices = {}
        self.__output_indices = {}
        # get arugments indices:
        # arguments names:
        #rois
        #features
        #roiLabels

        #outputs names:
        #ce_output
        #errs_output
        #z_output

        for arg, i in zip(self.__model.arguments, range(len(self.__model.arguments))):
            self.__args_indices[arg.name] = i
        for out, i in zip(self.__model.outputs, range(len(self.__model.outputs))):
            self.__output_indices[out.name] = i 

        self.__nr_rois = self.__model.arguments[self.__args_indices["rois"]].shape[0]
        self.__resize_width = self.__model.arguments[self.__args_indices["features"]].shape[1]
        self.__resize_height = self.__model.arguments[self.__args_indices["features"]].shape[2]
        self.labels_count = self.__model.arguments[self.__args_indices["roiLabels"]].shape[1]

    def resize_and_pad(self, img):
        self.ensure_model_is_loaded()

        # port of the c++ code from CNTK: https://github.com/Microsoft/CNTK/blob/f686879b654285d06d75c69ee266e9d4b7b87bc4/Source/Readers/ImageReader/ImageTransformers.cpp#L316
        img_width = len(img[0])
        img_height = len(img)

        scale_w = img_width > img_height

        target_w = self.__resize_width
        target_h = self.__resize_height

        if scale_w:
            target_h = int(np.round(img_height * float(self.__resize_width) / float(img_width)))
        else:
            target_w = int(np.round(img_width * float(self.__resize_height) / float(img_height)))

        resized = cv2.resize(img, (target_w, target_h), 0, 0, interpolation=cv2.INTER_NEAREST)

        top = int(max(0, np.round((self.__resize_height - target_h) / 2)))
        left = int(max(0, np.round((self.__resize_width - target_w) / 2)))

        bottom = self.__resize_height - top - target_h
        right = self.__resize_width - left - target_w

        resized_with_pad = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                              cv2.BORDER_CONSTANT, value=self.__pad_value_rgb)

        # tranpose(2,0,1) converts the image to the HWC format which CNTK accepts
        model_arg_rep = np.ascontiguousarray(np.array(resized_with_pad, dtype=np.float32).transpose(2, 0, 1))

        return resized_with_pad, model_arg_rep

    def get_rois_for_image(self, img):
        self.ensure_model_is_loaded()

        # get rois
        if self.__use_selective_search_rois:
            rects, scaled_img, scale = getSelectiveSearchRois(img, ss_scale, ss_sigma, ss_minSize,
                                                              roi_maxImgDim)  # interpolation=cv2.INTER_AREA
        else:
            rects = []
            scaled_img, scale = imresizeMaxDim(img, roi_maxImgDim, boUpscale=True, interpolation=cv2.INTER_AREA)

        imgWidth, imgHeight = imArrayWidthHeight(scaled_img)

        if not self.__use_selective_search_rois:
            if (imgWidth, imgHeight) in self.__rois_only_grid_cache:
                return self.__rois_only_grid_cache[(imgWidth, imgHeight)]

        # add grid rois
        if self.__use_grid_rois:
            if (imgWidth, imgHeight) in self.__grid_rois_cache:
                rectsGrid = self.__grid_rois_cache[(imgWidth, imgHeight)]
            else:
                rectsGrid = getGridRois(imgWidth, imgHeight, grid_nrScales, grid_aspectRatios)
                self.__grid_rois_cache[(imgWidth, imgHeight)] = rectsGrid

            rects += rectsGrid

        # run filter
        rois = filterRois(rects, imgWidth, imgHeight, roi_minNrPixels, roi_maxNrPixels, roi_minDim, roi_maxDim,
                              roi_maxAspectRatio)
        if len(rois) == 0:  # make sure at least one roi returned per image
            rois = [[5, 5, imgWidth - 5, imgHeight - 5]]

        # scale up to original size and save to disk
        # note: each rectangle is in original image format with [x,y,x2,y2]
        original_rois = np.int32(np.array(rois) / scale)

        img_width = len(img[0])
        img_height = len(img)

        # all rois need to be scaled + padded to cntk input image size
        targetw, targeth, w_offset, h_offset, scale = roiTransformPadScaleParams(img_width, img_height,
                                                                                 self.__resize_width,
                                                                                 self.__resize_height)

        rois = []
        for original_roi in original_rois:
            x, y, x2, y2 = roiTransformPadScale(original_roi, w_offset, h_offset, scale)

            xrel = float(x) / (1.0 * targetw)
            yrel = float(y) / (1.0 * targeth)
            wrel = float(x2 - x) / (1.0 * targetw)
            hrel = float(y2 - y) / (1.0 * targeth)

            rois.append([xrel, yrel, wrel, hrel])

        # pad rois if needed:
        if len(rois) < self.__nr_rois:
            rois += [[0, 0, 0, 0]] * (self.__nr_rois - len(rois))
        elif len(rois) > self.__nr_rois:
            rois = rois[:self.__nr_rois]

        if not self.__use_selective_search_rois:
            self.__rois_only_grid_cache[(imgWidth, imgHeight)] = (np.array(rois), original_rois)
        return np.array(rois), original_rois

    def detect(self, img):

        watch = StopWatch(silent=True)
        self.ensure_model_is_loaded()
        watch.t("Loaded model")

        self.warm_up()

        watch.t("Warmed up model")

        resized_img, img_model_arg = self.resize_and_pad(img)

        watch.t("Resized image")

        test_rois, original_rois = self.get_rois_for_image(img)

        watch.t("Generated ROIs")

        roi_padding_index = len(original_rois)

        # a dummy variable for labels the will be given as an input to the network but will be ignored
        dummy_labels = np.zeros((self.__nr_rois, self.labels_count))

        # prepare the arguments
        arguments = {
            self.__model.arguments[self.__args_indices["features"]]: [img_model_arg],
            self.__model.arguments[self.__args_indices["rois"]]: [test_rois],
            self.__model.arguments[self.__args_indices["roiLabels"]]: [dummy_labels]
        }

        # run it through the model
        output = self.__model.eval(arguments)
        watch.t("Evaluated through network")
        self.__model_warm  = True
        
        
        # some CNTK version call the output layer "z_output" and some "z", not sure why its not embdded in the model
        output_param_name = "z_output"
        if (not output_param_name in self.__output_indices):
            output_param_name = "z"
        
        # take just the relevant part and cast to float64 to prevent overflow when doing softmax
        rois_values = output[self.__model.outputs[self.__output_indices[output_param_name]]][0][0][:roi_padding_index].astype(np.float64)

        # get the prediction for each roi by taking the index with the maximal value in each row
        rois_labels_predictions = np.argmax(rois_values, axis=1)

        # calculate the probabilities using softmax
        rois_probs = softmax2D(rois_values)

        # TODO: Should we perform non maxima supression here? There's also another implementation that we use
        # in the calling code..
        non_padded_rois = test_rois[:roi_padding_index]
        max_probs = np.amax(rois_probs, axis=1).tolist()

        watch.t("Calculated probs")

        rois_prediction_indices = applyNonMaximaSuppression(nms_threshold, rois_labels_predictions, max_probs,
                                                            non_padded_rois)
        watch.t("non-maxima supression")

        #original_rois_predictions = original_rois[np.array(rois_labels_predictions[rois_prediction_indices]  == 1)]
        original_rois_predictions = original_rois[rois_prediction_indices]

        rois_predictions_labels = rois_labels_predictions[rois_prediction_indices]
        non_noise_indices = rois_predictions_labels > 0
        self.rois_predictions_labels = rois_predictions_labels[non_noise_indices]
        self.__rois_predictions = original_rois_predictions[non_noise_indices]
        return self.__rois_predictions

if __name__ == "__main__":
    import argparse
    import os
    parser = argparse.ArgumentParser(description='FRCNN Detector')
    parser.add_argument('--input', type=str, metavar='<path>',
                        help='Path to image file or to a directory containing image in jpg format', required=True)
    parser.add_argument('--output', type=str, metavar='<directory path>',
                        help='Path to output directory', required=False)
    parser.add_argument('--model', type=str, metavar='<file path>',
                        help='Path to model file',
                        required=True)

    parser.add_argument('--cntk-path', type=str, metavar='<dir path>',
                        help='Path to the diretory in which CNTK is installed, e.g. c:\\local',
                        required=False)

    parser.add_argument('--json-output', type=str, metavar='<file path>',
                        help='Path to output JSON file', required=False)

    args = parser.parse_args()

    input_path = args.input
    output_path = args.output
    json_output_path = args.json_output
    model_file_path = args.model

    if args.cntk_path:
        cntk_path = args.cntk_path
    else:
        cntk_path = "C:\\local"
    cntk_scripts_path = path.join(cntk_path, r"cntk/Examples/Image/Detection/FastRCNN")

    if (output_path is None and json_output_path is None):
        parser.error("No directory output path or json output path specified")

    if (output_path is not None) and not os.path.exists(output_path):
        os.makedirs(output_path)
    
    if os.path.isdir(input_path):
        import glob
        file_paths = glob.glob(os.path.join(input_path, '*.jpg'))
    else:
        file_paths = [input_path]

    detector = FRCNNDetector(model_file_path, use_selective_search_rois=False, 
                            cntk_scripts_path=cntk_scripts_path)
    detector.load_model()

    if (json_output_path is not None):
        model_classes = get_classes_description(model_file_path, detector.labels_count)
        json_output_obj = {"classes": model_classes,
                           "frames" : {}}

    colors = [(0,0,0), (255,0,0), (0,0,255)]
    players_label = -1
    print("Number of images to process: %d"%len(file_paths))

    for file_path, counter in zip(file_paths, range(len(file_paths))):
        img = cv2.imread(file_path)
        rects = detector.detect(img)

        print("Processed image %d"%(counter+1))

        if (output_path is not None):
            img_cpy = img.copy()

            print("Running FRCNN detection on", file_path)

            if players_label == -1:
                players_label = detector.labels_count - 1

            print("%d regions were detected"%(len(rects)))
            for rect, label in zip(rects, detector.rois_predictions_labels):
                x1, y1, x2, y2 = rect
                #if label == players_label:
                cv2.rectangle(img_cpy, (x1, y1), (x2, y2), (0,255,0), 2)

            output_file_path = os.path.join(output_path, os.path.basename(file_path))
            cv2.imwrite(output_file_path, img_cpy)
        elif (json_output_path is not None):
            image_base_name = path.basename(file_path)
            regions_list = []
            json_output_obj["frames"][image_base_name] = {"regions": regions_list}
            for rect, label in zip(rects, detector.rois_predictions_labels):
                regions_list.append({
                    "x1" : int(rect[0]),
                    "y1" : int(rect[1]),
                    "x2" : int(rect[2]),
                    "y2" : int(rect[3]),
                    "class" : int(label)
                })

    if (json_output_path is not None):
        with open(json_output_path, "wt") as handle:
            json_dump = json.dumps(json_output_obj, indent=2)
            handle.write(json_dump)





