# pylint: disable=unsubscriptable-object
import glob
import os
import pickle
import itertools
import pdb

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

import deepfly.logger as logger
import deepfly.skeleton_fly as skeleton
from deepfly.Camera import Camera
from deepfly.Config import config
from deepfly.cv_util import triangulate_linear
from deepfly.os_util import read_calib, read_camera_order
from deepfly.os_util import parse_img_name
import json
import deepfly.optimise_triangulation as optimise_triangulation


def load_pred_from_json(path_json, folder_name, num_images):
    json_data = json.load(open(path_json, "r"))
    pred = np.zeros((config["num_cameras"] + 1, num_images, skeleton.num_joints//2, 2))

    for session_id in json_data.keys():
        if folder_name in json_data[session_id]["data"]:
            for image_name in json_data[session_id]["data"][folder_name].keys():

                cid, img_id = parse_img_name(image_name)
                anot = json_data[session_id]["data"][folder_name][image_name][
                    "position"
                ]

                pred[cid, img_id, :15] = anot[:15]

    return pred


def pred2pred_cam(pred, cam_id, cam_id_read, image_shape, num_images):
    if pred is None:
        return None
    num_images_in_pred = num_images
    pred_cam = np.zeros(shape=(pred.shape[1], skeleton.num_joints, 2), dtype=float)
    num_joints = skeleton.num_joints

    if cam_id > 3:
        pred_cam[:num_images_in_pred, num_joints // 2 :, :] = (
            pred[cam_id_read, :num_images_in_pred] * image_shape
        )
    elif cam_id == 3:
        pred_cam[:num_images_in_pred, : num_joints // 2, :] = (
            pred[cam_id_read, :num_images_in_pred] * image_shape
        )
        if pred.shape[0] > 7:
            pred_cam[:num_images_in_pred, num_joints // 2 :, :] = (
                pred[7, :num_images_in_pred] * image_shape
            )
    elif cam_id < 3:
        pred_cam[:num_images_in_pred, : num_joints // 2, :] = (
            pred[cam_id_read, :num_images_in_pred] * image_shape
        )

    return pred_cam


def find_pred_path(path_folder):
    pred_path_list = glob.glob(os.path.join(path_folder, "pred*.pkl"))
    pred_path_list.sort(key=os.path.getmtime)
    pred_path_list = pred_path_list[::-1]

    return pred_path_list[0] if len(pred_path_list) else None


def find_hm_path(path_folder):
    heatmap_path_list = glob.glob(os.path.join(path_folder, "heatmap*.pkl"))
    heatmap_path_list.sort(key=os.path.getmtime)
    heatmap_path_list = heatmap_path_list[::-1]

    return heatmap_path_list[0] if len(heatmap_path_list) else None


def load_heatmap(hm_path, shape):
    logger.debug("Heatmap shape: {}".format(shape))
    heatmap = np.memmap(filename=hm_path, mode="r", shape=shape, dtype="float32")

    return heatmap


class CameraNetwork:
    def __init__(
        self,
        image_folder,
        output_folder,
        cam_list=None,
        calibration=None,
        num_images=900,
        cam_id_list=range(config["num_cameras"]),
        cid2cidread=None,
        heatmap=None,
        pred=None,
        hm_path=None,
        pred_path=None,
    ):
        self.folder = image_folder
        self.folder_output = output_folder
        self.dict_name = image_folder
        self.points3d_m = None
        self.bone_param = None
        self.num_images = num_images
        self.num_joints = config["skeleton"].num_joints
        self.heatmap_shape = config["heatmap_shape"]
        self.image_shape = config["image_shape"]
        self.num_cameras = len(cam_id_list)

        if cid2cidread is not None:
            self.cid2cidread = cid2cidread
        else:
            _, cid2cidread = read_camera_order(self.folder_output)
            self.cid2cidread = cid2cidread

        if cam_list:
            logger.debug("Camera list is already given, skipping loading.")
            self.cam_list = cam_list
        else:
            self.cam_list = list()
            if pred_path is None:
                pred_path = find_pred_path(self.folder_output)
            if pred_path is None:
                logger.debug("no pred file under {}".format(self.folder_output))
            if pred is None and pred_path is not None:
                logger.debug("loading pred path {}".format(pred_path))
                if pred_path.endswith(".json"):
                    pred = load_pred_from_json(
                        pred_path, os.path.basename(image_folder), self.num_images
                    )
                else:
                    pred = np.load(file=pred_path, mmap_mode="r", allow_pickle=True)

            num_images_in_pred = pred.shape[1] if pred is not None else num_images

            if hm_path is None:
                hm_path = find_hm_path(self.folder_output)
            if hm_path is None:
                logger.debug("no heatmap file under {}".format(self.folder_output))
            if heatmap is None and hm_path is not None:
                hm_shape = (
                    config["num_cameras"] + 1,
                    num_images_in_pred,
                    config["num_predict"],
                    self.heatmap_shape[0],
                    self.heatmap_shape[1],
                )
                logger.debug("Heatmap shape: {}".format(hm_shape))
                logger.debug("Reading hm from {}".format(hm_path))
                heatmap = load_heatmap(hm_path, hm_shape)

            if self.num_images is not None and self.num_images < num_images_in_pred:
                if pred is not None:
                    pred = pred[:, : self.num_images]
                if heatmap is not None:
                    heatmap = heatmap[:, : self.num_images]

            for cam_id in cam_id_list:
                cam_id_read = cid2cidread[cam_id]
                pred_cam = pred2pred_cam(
                    pred, cam_id, cam_id_read, self.image_shape, num_images_in_pred
                )
                self.cam_list.append(
                    Camera(
                        cid=cam_id,
                        cid_read=cam_id_read,
                        image_folder=image_folder,
                        hm=heatmap,
                        points2d=pred_cam,
                    )
                )

        if calibration is None:
            logger.debug("Reading calibration from {}".format(self.folder_output))
            calibration = read_calib(self.folder_output)
        if calibration is not None:
            _ = self.load_network(calibration)

    def set_cid2cidread(self, cid2cidread):
        assert len(self.cam_list) == len(cid2cidread)
        self.cid2cidread = cid2cidread
        for cam, cidread in zip(self.cam_list, cid2cidread):
            cam.cam_id_read = cidread

    def has_calibration(self):
        return np.all([c.P is not None for c in self.cam_list])

    def has_pose(self):
        return self.cam_list[0].points2d is not None

    def has_heatmap(self):
        return self.cam_list[0].hm is not None

    def triangulate(self, cam_id_list=None, anipose_optimise_3d=False, reprojection_error_optimisation=False, graph_reprojection_errors=False, save_all_options=False):
        assert self.cam_list
        count = 0

        if True: #not anipose_optimise_3d:
            if cam_id_list is None:
                cam_id_list = list(range(self.num_cameras))
            points2d_shape = self.cam_list[0].points2d.shape
            self.points3d_m = np.zeros(
                shape=(points2d_shape[0], points2d_shape[1], 3), dtype=np.float
            )
            data_shape = self.cam_list[0].points2d.shape
            reproj_metrics_list = []
            reprojection_error_distance = np.zeros([7, data_shape[0], data_shape[1]])
            reprojection_error_log = open(os.path.join(self.folder, 'df3d', 'reprojection_errors.log'), "w")
            reprojection_error_optim_camera_not_used = np.zeros([data_shape[0], data_shape[1]])
            reprojection_error_optim_camera_not_used[:] = np.nan
            points3d_df3d_raw = self.points3d_m.copy()
            points3d_df3d_optim = self.points3d_m.copy()
            points3d_anipose_with_raw = self.points3d_m.copy()
            points3d_anipose_with_optim = self.points3d_m.copy()
            for img_id in range(data_shape[0]):
                for j_id in range(data_shape[1]):
                    cam_list_iter = list()
                    points2d_iter = list()
                    for cam in [self.cam_list[cam_idx] for cam_idx in cam_id_list]:
                        if np.any(cam[img_id, j_id, :] == 0):
                            continue
                        if not config["skeleton"].camera_see_joint(cam.cam_id, j_id):
                            #this line removes the cameras that can't see this joint
                            continue
                        cam_list_iter.append(cam)
                        points2d_iter.append(cam[img_id, j_id, :])

                    if len(cam_list_iter) >= 2:
                        #self.points3d_m is 1400,38,3
                        #cam_list_iter is 3 cameras
                        #points2d_iter is 3 2d points
                        df3d_points = triangulate_linear(cam_list_iter, points2d_iter)
                        self.points3d_m[img_id, j_id, :] = df3d_points.copy()
                        points3d_df3d_raw[img_id, j_id, :] = df3d_points.copy()
                        points3d_df3d_optim[img_id, j_id, :] = df3d_points.copy()
                        if reprojection_error_optimisation:
                            errs = []
                            full_errs = []
                            squares_errs = []
                            distance_errs = []
                            for c in cam_list_iter:
                                full_err = c.reprojection_error_given(np.expand_dims(self.points3d_m[img_id, j_id, :], axis=0), np.expand_dims(c.points2d[img_id, j_id, :], axis=0))
                                errs.append(full_err[0])
                                full_errs.append(full_err)
                                distance_errs.append(np.linalg.norm(full_err[1]))
                                squares_errs.append(full_err[1][0] ** 2 + full_err[1][1] ** 2)
                                reprojection_error_distance[c.cam_id, img_id, j_id] = np.linalg.norm(full_err[1])
                            err = np.mean(distance_errs) # TODO I really dont think that using the mean reprojection error is a good idea
                            if err > 25 and len(cam_list_iter) > 2 and j_id in [i for i in range(0,15)] + [i for i in range(19, 34)]: # 30 -> 0
                                #print("reproj optim on frame %d"%(img_id))
                                assert len(cam_list_iter) == 3
                                print("reprojection error for frame:%04d, joint:%02d, cameras:[%d,%d,%d], was > threshold:%d"%(img_id, j_id, cam_list_iter[0].cam_id, cam_list_iter[1].cam_id, cam_list_iter[2].cam_id, 25), file=reprojection_error_log)
                                count += 1
                                # find best from just two cameras
                                # determine which camera to delete
                                assert len(distance_errs) == 3
                                reproj_metrics_single = {'tri-camera-3d':self.points3d_m[img_id, j_id, :].copy()}

                                #Determination of the best two cameras for 3d triangulation
                                diff1 = np.abs(np.max(distance_errs) - np.median(distance_errs))
                                diff2 = np.abs(np.median(distance_errs) - np.min(distance_errs))
                                c1_i = distance_errs.index(np.median(distance_errs))
                                c2_i                = distance_errs.index(np.min(distance_errs)) if diff2 < diff1 else distance_errs.index(np.max(distance_errs))
                                unused_camera_index = distance_errs.index(np.min(distance_errs)) if diff2 > diff1 else distance_errs.index(np.max(distance_errs))
                                reprojection_error_optim_camera_not_used[img_id, j_id] = int(unused_camera_index)
                                p1 = (cam_list_iter[c1_i], points2d_iter[c1_i])
                                p2 = (cam_list_iter[c2_i], points2d_iter[c2_i])
                                optim_3d_points = triangulate_linear([p1[0], p2[0]], [p1[1], p2[1]])

                                #Determination of the best two cameras for 3d triangulation
                                '''
                                best_camera_pair = cam_list_iter
                                best_pts_2d = points2d_iter
                                current_err = err
                                optim_3d_points = 0
                                for p1, p2 in itertools.combinations(zip(cam_list_iter, points2d_iter), r=2):
                                    optim_3d_points = triangulate_linear([p1[0], p2[0]], [p1[1], p2[1]])
                                    reproj_errors = []
                                    for c in [p1[0], p2[0]]:
                                        full_err = c.reprojection_error_given(np.expand_dims(self.points3d_m[img_id, j_id, :], axis=0), np.expand_dims(c.points2d[img_id, j_id, :], axis=0))
                                        reproj_errors.append(np.linalg.norm(full_err[1]))
                                    if np.mean(reproj_errors) < current_err:
                                        current_err = np.mean(reproj_errors)
                                        best_camera_pair = [p1, p2]
                                assert len(best_camera_pair) == 2
                                p1 = best_camera_pair[0]
                                p2 = best_camera_pair[1]
                                '''
                                    
                                # Recording metrics
                                reproj_metrics_single['optim-3d-pts'] = optim_3d_points
                                reproj_metrics_single['all_cameras'] = cam_list_iter.copy()
                                reproj_metrics_single['chosen_cameras'] = [p1[0], p2[0]]
                                reproj_metrics_single['frame'] = img_id
                                reproj_metrics_single['joint'] = j_id

                                # 'Saving' result
                                self.points3d_m[img_id, j_id, :] = optim_3d_points
                                points3d_df3d_optim[img_id, j_id, :] = optim_3d_points.copy()

                                # Calculating new reprojection error
                                unused_cam_ids = {0,1,2,3,4,5,6}
                                for c in [p1[0], p2[0]]:
                                    unused_cam_ids -= {c.cam_id}
                                    full_err = c.reprojection_error_given(np.expand_dims(self.points3d_m[img_id, j_id, :], axis=0), np.expand_dims(c.points2d[img_id, j_id, :], axis=0))
                                    reprojection_error_distance[c.cam_id, img_id, j_id] = np.linalg.norm(full_err[1])
                                for cid in unused_cam_ids:
                                    reprojection_error_distance[cid, img_id, j_id] = 0
                                reproj_metrics_list.append(reproj_metrics_single)
                                
        if anipose_optimise_3d:
            assert len(self.cam_list) == 7
            assert np.max(self.cam_list[3].points2d) == 0. # camera 3 has no information
            cam_list_copy = self.cam_list.copy()
            del cam_list_copy[3] # remove camera 3 (not strictly necessary if accomedated for, but easier to do it here)

            # anipose 3d triangulation
            #self.points3d_m = optimise_triangulation.anipose_3d_triangulation(cam_list_copy)

            #add in spatio-temporal filtering/triangulation optimisation here
            anipose_shape_points2d = optimise_triangulation.reshape_2d(cam_list_copy)
            #self.points3d_m = optimise_triangulation.optimise_3d(cam_list_copy, anipose_shape_points2d, self.points3d_m)
            points3d_anipose_with_raw = optimise_triangulation.optimise_3d(cam_list_copy, anipose_shape_points2d.copy(), points3d_df3d_raw)

            if reprojection_error_optimisation:
                # remove 2d points detected to be wrong
                for frame, joint in itertools.product(range(data_shape[0]), range(data_shape[1])):
                    if not np.isnan(reprojection_error_optim_camera_not_used[frame, joint]):
                        anipose_shape_points2d[int(reprojection_error_optim_camera_not_used[frame, joint]), frame, joint, :] = np.nan
                points3d_anipose_with_optim = optimise_triangulation.optimise_3d(cam_list_copy, anipose_shape_points2d.copy(), points3d_df3d_optim)
                self.points3d_m = points3d_anipose_with_optim
            else:
                self.points3d_m = points3d_anipose_with_raw

        if save_all_options:
            np.save(os.path.join(self.folder, "df3d", "optimise_df3d_raw"), points3d_df3d_raw)
            np.save(os.path.join(self.folder, "df3d", "optimise_df3d_optim"), points3d_df3d_optim)
            np.save(os.path.join(self.folder, "df3d", "optimise_anipose_with_raw"), points3d_anipose_with_raw)
            np.save(os.path.join(self.folder, "df3d", "optimise_anipose_with_optim"), points3d_anipose_with_optim)

        if graph_reprojection_errors: # show reprojection errors:
            import matplotlib.pyplot as plt
            graph_data = []
            if reprojection_error_optimisation:
                graph_data = reprojection_error_distance
            else:
                # calculate reprojection error here
                graph_data = np.zeros([7, data_shape[0], data_shape[1]])
                for img_id, j_id in itertools.product(range(data_shape[0]), range(data_shape[1])):
                    cam_list_iter = list()
                    points2d_iter = list()
                    for cam in [self.cam_list[cam_idx] for cam_idx in cam_id_list]:
                        if np.any(cam[img_id, j_id, :] == 0):
                            continue
                        if not config["skeleton"].camera_see_joint(cam.cam_id, j_id):
                            #this line removes the cameras that can't see this joint
                            continue
                        cam_list_iter.append(cam)
                        points2d_iter.append(cam[img_id, j_id, :])
                        for c in cam_list_iter:
                            full_err = c.reprojection_error_given(np.expand_dims(self.points3d_m[img_id, j_id, :], axis=0), np.expand_dims(c.points2d[img_id, j_id, :], axis=0))
                            graph_data[c.cam_id, img_id, j_id] = np.linalg.norm(full_err[1])
                

            for i, colour in zip(range(0,7), ["#FF0000","#00FF00","#0000FF","#00FFFF","#000000","#FF00FF","#FFFF00"]):
                for j in [k for k in range(0,15)] + [k for k in range(19, 34)]:
                    linestyle='.'
                    if j in [4,9,14,23,28,33]:
                        linestyle='-'
                    plt.plot(graph_data[i, :, j], color=colour)
            plt.show()
            pdb.set_trace()
            
        if False: # Show reprojection changes
            import matplotlib.pyplot as plt
            import sys
            import skimage.io
            sys.path.append("..")
            from Metrics.dataloader import Metrics

            def plot_single_pose(ax, camera, img_id, j_id, pts3d):
                fname = os.path.join(self.folder, "camera_%d_img_%06d.jpg"%(camera.cam_id, img_id))
                ax.imshow(skimage.io.imread(fname))
                offsets = Metrics.offsets
                if camera.cam_id < 3:
                    legs_list = Metrics.legs_list[:3]
                else:
                    legs_list = Metrics.legs_list[3:]

                reprojection_2d = camera.project(pts3d)
                #import pdb
                #pdb.set_trace()
                ax.scatter(reprojection_2d[0][0], reprojection_2d[0][1], c=Metrics.joint_colours[j_id])
                ax.scatter(camera.points2d[img_id, j_id, 0], camera.points2d[img_id, j_id, 1], c="white")

            for reproj in reproj_metrics_list:
                cam_discarded = [c for c in reproj['all_cameras'] if c not in reproj['chosen_cameras']][0]
                print("reprojection error correction, frame:%d, joint:%d, camera discarded:%d"%(reproj['frame'], reproj['joint'], cam_discarded.cam_id))
                f, axs = plt.subplots(2,3)
                for j in range(0, 3):
                    plot_single_pose(axs[0,j], reproj['all_cameras'][j], reproj['frame'], reproj['joint'], reproj['tri-camera-3d'])
                for j in range(0, 3):
                    plot_single_pose(axs[1,j], reproj['all_cameras'][j], reproj['frame'], reproj['joint'], self.points3d_m[reproj['frame'], reproj['joint'], :])
                plt.show()
                
            

        print("%d joints changed with reprojection optim"%(count))

    def reprojection_error(self, cam_indices=None, ignore_joint_list=None):
        if ignore_joint_list is None:
            ignore_joint_list = config["skeleton"].ignore_joint_id
        if cam_indices is None:
            cam_indices = range(len(self.cam_list))

        err_list = list()
        for (img_id, j_id, _), _ in np.ndenumerate(self.points3d_m):
            p3d = self.points3d_m[img_id, j_id].reshape(1, 3)
            if j_id in ignore_joint_list:
                continue
            for cam in self.cam_list:
                if not config["skeleton"].camera_see_joint(cam.cam_id, j_id):
                    continue
                err_list.append((cam.project(p3d) - cam[img_id, j_id]).ravel())

        err_mean = np.mean(np.abs(err_list))
        logger.debug("Ignore_list {}:  {:.4f}".format(ignore_joint_list, err_mean))
        return err_list

    def prepare_bundle_adjust_param(
        self, camera_id_list=None, ignore_joint_list=None, max_num_images=1000
    ):
        logger.debug("Calibration ignore joint list {}".format(ignore_joint_list))
        if ignore_joint_list is None:
            ignore_joint_list = config["skeleton"].ignore_joint_id
        if camera_id_list is None:
            camera_id_list = list(range(self.num_cameras))

        camera_params = np.zeros(shape=(len(camera_id_list), 13), dtype=float)
        cam_list = [self.cam_list[c] for c in camera_id_list]
        for i, cid in enumerate(camera_id_list):
            camera_params[i, 0:3] = np.squeeze(cam_list[cid].rvec)
            camera_params[i, 3:6] = np.squeeze(cam_list[cid].tvec)
            camera_params[i, 6] = cam_list[cid].focal_length_x
            camera_params[i, 7] = cam_list[cid].focal_length_y
            camera_params[i, 8:13] = np.squeeze(cam_list[cid].distort)

        point_indices = []
        camera_indices = []
        points2d_ba = []
        points3d_ba = []
        points3d_ba_source = dict()
        points3d_ba_source_inv = dict()
        point_index_counter = 0
        data_shape = self.points3d_m.shape

        if data_shape[0] > max_num_images:
            logger.debug(
                "There are too many ({}) images for calibration. Selecting {} randomly.".format(
                    data_shape[0], max_num_images
                )
            )
            img_id_list = np.random.randint(
                0, high=data_shape[0] - 1, size=(max_num_images)
            )
        else:
            logger.debug("Using {} images for calibration".format(data_shape[0]))
            img_id_list = np.arange(data_shape[0] - 1)

        for img_id in img_id_list:
            for j_id in range(data_shape[1]):
                cam_list_iter = list()
                points2d_iter = list()
                for cam in cam_list:
                    if j_id in ignore_joint_list:
                        continue
                    if np.any(self.points3d_m[img_id, j_id, :] == 0):
                        continue
                    if np.any(cam[img_id, j_id, :] == 0):
                        continue
                    if not config["skeleton"].camera_see_joint(cam.cam_id, j_id):
                        continue
                    if cam.cam_id == 3:
                        continue
                    cam_list_iter.append(cam)
                    points2d_iter.append(cam[img_id, j_id, :])

                # the point is seen by at least two cameras, add it to the bundle adjustment
                if len(cam_list_iter) >= 2:
                    points3d_iter = self.points3d_m[img_id, j_id, :]
                    points2d_ba.extend(points2d_iter)
                    points3d_ba.append(points3d_iter)
                    point_indices.extend([point_index_counter] * len(cam_list_iter))
                    points3d_ba_source[(img_id, j_id)] = point_index_counter
                    points3d_ba_source_inv[point_index_counter] = (img_id, j_id)
                    point_index_counter += 1
                    camera_indices.extend([cam.cam_id for cam in cam_list_iter])

        c = 0
        # make sure stripes from both sides share the same point id's
        # TODO move this into config file
        if "fly" in config["name"]:
            for idx, point_idx in enumerate(point_indices):
                img_id, j_id = points3d_ba_source_inv[point_idx]
                if (
                    config["skeleton"].is_tracked_point(
                        j_id, config["skeleton"].Tracked.STRIPE
                    )
                    and j_id > config["skeleton"].num_joints // 2
                ):
                    if (
                        img_id,
                        j_id - config["skeleton"].num_joints // 2,
                    ) in points3d_ba_source:
                        point_indices[idx] = points3d_ba_source[
                            (img_id, j_id - config["skeleton"].num_joints // 2)
                        ]
                        c += 1

        logger.debug("Replaced {} points".format(c))
        points3d_ba = np.squeeze(np.array(points3d_ba))
        points2d_ba = np.squeeze(np.array(points2d_ba))
        cid2cidx = {v: k for (k, v) in enumerate(np.sort(np.unique(camera_indices)))}
        camera_indices = [cid2cidx[cid] for cid in camera_indices]
        camera_indices = np.array(camera_indices)
        point_indices = np.array(point_indices)

        n_cameras = camera_params.shape[0]
        n_points = points3d_ba.shape[0]

        x0 = np.hstack((camera_params.ravel(), points3d_ba.ravel()))

        return (
            x0.copy(),
            points2d_ba.copy(),
            n_cameras,
            n_points,
            camera_indices,
            point_indices,
        )

    def bundle_adjust(
        self, cam_id_list=None, ignore_joint_list=config["skeleton"].ignore_joint_id
    ):
        assert self.cam_list
        if cam_id_list is None:
            cam_id_list = range(self.num_cameras)

        self.reprojection_error(
            cam_indices=cam_id_list, ignore_joint_list=ignore_joint_list
        )
        x0, points_2d, n_cameras, n_points, camera_indices, point_indices = self.prepare_bundle_adjust_param(
            cam_id_list, ignore_joint_list=ignore_joint_list
        )
        logger.debug(f"Number of points for calibration: {n_points}")
        A = bundle_adjustment_sparsity(
            n_cameras, n_points, camera_indices, point_indices
        )
        res = least_squares(
            residuals,
            x0,
            jac_sparsity=A,
            verbose=2 if logger.debug_enabled() else 0,
            x_scale="jac",
            ftol=1e-4,
            method="trf",
            args=(
                [self.cam_list[i] for i in cam_id_list],
                n_cameras,
                n_points,
                camera_indices,
                point_indices,
                points_2d,
            ),
            max_nfev=1000,
        )

        logger.debug(
            "Bundle adjustment, Average reprojection error: {}".format(
                np.mean(np.abs(res.fun))
            )
        )

        self.triangulate(cam_id_list)
        return res

    def save_network(self, path, meta=None):
        if path is not None and os.path.exists(path):  # to prevent overwriting
            d = pickle.load(open(path, "rb"))
        else:
            d = {cam_id: dict() for cam_id in np.arange(0, 7)}
            d["meta"] = meta

        for cam in self.cam_list:
            d[cam.cam_id]["R"] = cam.R
            d[cam.cam_id]["tvec"] = cam.tvec
            d[cam.cam_id]["intr"] = cam.intr
            d[cam.cam_id]["distort"] = cam.distort

        if path is not None:
            pickle.dump(d, open(path, "wb"))

        return d

    def load_network(self, calib):
        d = calib
        if calib is None:
            return None
        for cam in self.cam_list:
            if cam.cam_id in d and d[cam.cam_id]:
                cam.set_R(d[cam.cam_id]["R"])
                cam.set_tvec(d[cam.cam_id]["tvec"])
                cam.set_intrinsic(d[cam.cam_id]["intr"])
                cam.set_distort(d[cam.cam_id]["distort"])
            else:
                logger.debug(
                    "Camera {} is not on the calibration file".format(cam.cam_id)
                )

        return d["meta"]

    def get_points2d_matrix(self):
        pts2d = np.zeros((7, self.num_images, config["num_joints"], 2), dtype=float)

        for cam in self.cam_list:
            pts2d[cam.cam_id, :] = cam.points2d.copy()

        return pts2d

    def set_points2d_matrix(self, pts2d):
        for cam in self.cam_list:
            cam.points2d[:] = pts2d[cam.cam_id]

    """
    STATIC
    """

    @staticmethod
    def calc_essential_matrix(points2d_1, points2d_2, intr):
        E, mask = cv2.findEssentialMat(
            points1=points2d_1,
            points2=points2d_2,
            cameraMatrix=intr,
            method=cv2.RANSAC,
            prob=0.9999,
            threshold=5,
        )
        logger.debug(
            "Essential matrix inlier ratio: {}".format(np.sum(mask) / mask.shape[0])
        )
        return E, mask

    @staticmethod
    def calc_Rt_from_essential(E, points1, points2, intr):
        _, R, t, mask, _ = cv2.recoverPose(
            E, points1=points1, points2=points2, cameraMatrix=intr, distanceThresh=100
        )
        return R, t, mask


def residuals(
    params,
    cam_list,
    n_cameras,
    n_points,
    camera_indices,
    point_indices,
    points_2d,
    residual_mask=None,
):
    """Compute residuals.
    `params` contains camera parameters and 3-D coordinates.
    """
    assert point_indices.shape[0] == points_2d.shape[0]
    assert camera_indices.shape[0] == points_2d.shape[0]

    camera_params = params[: n_cameras * 13].reshape((n_cameras, 13))
    points3d = params[n_cameras * 13 :].reshape((n_points, 3))
    cam_indices_list = list(set(camera_indices))

    points_proj = np.zeros(shape=(point_indices.shape[0], 2), dtype=np.float)
    for cam_id in cam_indices_list:
        cam_list[cam_id].set_rvec(camera_params[cam_id][0:3])
        cam_list[cam_id].set_tvec(camera_params[cam_id][3:6])

        points2d_mask = camera_indices == cam_id
        points3d_where = point_indices[points2d_mask]
        points_proj[points2d_mask, :] = cam_list[cam_id].project(
            points3d[points3d_where]
        )

    res = points_proj - points_2d
    res = res.ravel()
    if residual_mask is not None:
        res *= residual_mask

    return res


def bundle_adjustment_sparsity(n_cameras, n_points, camera_indices, point_indices):
    assert camera_indices.shape[0] == point_indices.shape[0]
    n_camera_params = 13
    m = camera_indices.size * 2
    # all the parameters, 13 camera parameters and x,y,z values for n_points
    n = n_cameras * n_camera_params + n_points * 3
    A = lil_matrix((m, n), dtype=int)  # sparse matrix
    i = np.arange(camera_indices.size)

    for s in range(n_camera_params):
        # assign camera parameters to points residuals (reprojection error)
        A[2 * i, camera_indices * n_camera_params + s] = 1
        A[2 * i + 1, camera_indices * n_camera_params + s] = 1

    for s in range(3):
        # assign 3d points to residuals (reprojection error)
        A[2 * i, n_cameras * n_camera_params + point_indices * 3 + s] = 1
        A[2 * i + 1, n_cameras * n_camera_params + point_indices * 3 + s] = 1

    return A
