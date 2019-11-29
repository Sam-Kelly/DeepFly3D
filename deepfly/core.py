import os.path
import math  # inf
import numpy as np

from deepfly.utils_ramdya_lab import find_default_camera_ordering
from deepfly.GUI.CameraNetwork import CameraNetwork
from deepfly.GUI.Config import config
from deepfly.GUI.util.os_util import write_camera_order, read_camera_order, read_calib, get_max_img_id
from deepfly.GUI.util.optim_util import energy_drosoph
from deepfly.pose2d import ArgParse
from deepfly.pose2d.drosophila import main as pose2d_main
from deepfly.pose3d.procrustes.procrustes import procrustes_seperate
import pickle


class Core:
    def __init__(self, input_folder, num_images_max):
        self.input_folder = input_folder
        self.output_folder = os.path.join(self.input_folder, 'df3d/')
        
        self.num_images_max = num_images_max or math.inf
        max_img_id = get_max_img_id(self.input_folder)
        self.num_images = min(self.num_images_max, max_img_id + 1)
        self.max_img_id = self.num_images - 1

        self.setup_camera_ordering()
        self.set_cameras()
        

    @property
    def input_folder(self): 
        return self._input_folder


    @input_folder.setter 
    def input_folder(self, value): 
        value = os.path.abspath(value)
        value = value.rstrip('/')
        assert os.path.isdir(value), f'Not a directory {value}'
        self._input_folder = value 


    @property
    def output_folder(self): 
        return self._output_folder


    @output_folder.setter 
    def output_folder(self, value): 
        os.makedirs(value, exist_ok=True)
        value = os.path.abspath(value)
        value = value.rstrip('/')
        assert os.path.isdir(value), f'Not a directory {value}'
        self._output_folder = value 


    def setup_camera_ordering(self):
        default = find_default_camera_ordering(self.input_folder)
        if default is not None:  # np.arrays don't evaluate to bool
            write_camera_order(self.output_folder, default)
        self.cidread2cid, self.cid2cidread = read_camera_order(self.output_folder)


    def update_camera_ordering(self, cidread2cid):
        if cidread2cid is None:
            return False

        if len(cidread2cid) != config["num_cameras"]:
            print(f"Cannot rename images as there are no {config['num_cameras']} values")
            return False

        print("Camera order {}".format(cidread2cid))
        write_camera_order(self.output_folder, cidread2cid)
        self.cidread2cid, self.cid2cidread = read_camera_order(self.output_folder)
        self.camNetAll.set_cid2cidread(self.cid2cidread)
        return True


    def set_cameras(self):
        calib = read_calib(self.output_folder)
        self.camNetAll = CameraNetwork(
            image_folder=self.input_folder,
            output_folder=self.output_folder,
            cam_id_list=range(config["num_cameras"]),
            cid2cidread=self.cid2cidread,
            num_images=self.num_images,
            calibration=calib,
            num_joints=config["skeleton"].num_joints,
            heatmap_shape=config["heatmap_shape"],
        )
        self.camNetLeft = CameraNetwork(
            image_folder=self.input_folder,
            output_folder=self.output_folder,
            cam_id_list=config["left_cameras"],
            num_images=self.num_images,
            calibration=calib,
            num_joints=config["skeleton"].num_joints,
            cid2cidread=[self.cid2cidread[cid] for cid in config["left_cameras"]],
            heatmap_shape=config["heatmap_shape"],
            cam_list=[cam for cam in self.camNetAll if cam.cam_id in config["left_cameras"]],
        )
        self.camNetRight = CameraNetwork(
            image_folder=self.input_folder,
            output_folder=self.output_folder,
            cam_id_list=config["right_cameras"],
            num_images=self.num_images,
            calibration=calib,
            num_joints=config["skeleton"].num_joints,
            cid2cidread=[self.cid2cidread[cid] for cid in config["right_cameras"]],
            heatmap_shape=config["heatmap_shape"],
            cam_list=[self.camNetAll[cam_id] for cam_id in config["right_cameras"]],
        )

        self.camNetLeft.bone_param = config["bone_param"]
        self.camNetRight.bone_param = config["bone_param"]
        calib = read_calib(config["calib_fine"])
        self.camNetAll.load_network(calib)


    def pose2d_estimation(self):
        parser = ArgParse.create_parser()
        args, _ = parser.parse_known_args()
        args.checkpoint = False
        args.unlabeled = self.input_folder
        args.resume = config["resume"]
        args.stacks = config["num_stacks"]
        args.test_batch = config["batch_size"]
        args.img_res = [config["heatmap_shape"][0] * 4, config["heatmap_shape"][1] * 4]
        args.hm_res = config["heatmap_shape"]
        args.num_classes = config["num_predict"]
        args.max_img_id = self.max_img_id

        pose2d_main(args)   # will write output files in output directory
        self.set_cameras()  # makes sure cameras use the latest heatmaps and predictions


    def get_joint_reprojection_error(self, img_id, joint_id, camNet):
        visible_cameras = [
            cam
            for cam in camNet
            if config["skeleton"].camera_see_joint(cam.cam_id, joint_id)
        ]
        if len(visible_cameras) < 2:
            err_proj = 0
        else:
            pts = np.array([ cam.points2d[img_id, joint_id, :] for cam in visible_cameras ])
            _, err_proj, _, _ = energy_drosoph(visible_cameras, img_id, joint_id, pts / [960, 480])
        return err_proj


    def next_error(self, img_id, step=+1):
        joints = [j for j in range(config["skeleton"].num_joints) if j in config["skeleton"].pictorial_joint_list]
        last_id = 0 if step < 0 else self.max_img_id
        for img_id in range(img_id+step, last_id+step, step):
            for joint_id in joints:
                err_left  = self.get_joint_reprojection_error(img_id, joint_id, self.camNetLeft)
                err_right = self.get_joint_reprojection_error(img_id, joint_id, self.camNetRight)
                err = max(err_left, err_right)
                if err > config["reproj_thr"][joint_id]:
                    print(f"Error found at img={img_id} joint={joint_id} err={err}")
                    return img_id
        return last_id


    def calibrate_calc(self, drosophAnnot, min_img_id, max_img_id):
        print(f"Calibration considering frames between {min_img_id}:{max_img_id}")
        calib = read_calib(config["calib_fine"])
        assert calib is not None
        self.camNetAll.load_network(calib)

        # take a copy of the current points2d
        pts2d = np.zeros((config["num_cameras"], self.num_images, config["skeleton"].num_joints, 2), dtype=float)
        for cam_id in range(config["num_cameras"]):
            pts2d[cam_id, :] = self.camNetAll[cam_id].points2d.copy()

        # ugly hack to temporarly incorporate manual corrections to calibration
        c = 0
        for cam_id in range(config["num_cameras"]):
            for img_id in range(self.num_images):
                if drosophAnnot.state.db.has_key(cam_id, img_id):
                    pt = drosophAnnot.state.db.read(cam_id, img_id) * config["image_shape"]
                    self.camNetAll[cam_id].points2d[img_id, :] = pt
                    c += 1
        print("Calibration: replaced {} points from manuall correction".format(c))

        # keep the pts only in the range
        for cam in self.camNetAll:
            cam.points2d = cam.points2d[min_img_id:max_img_id, :]

        self.camNetLeft.triangulate()
        self.camNetLeft.bundle_adjust(cam_id_list=(0,1,2), unique=False, prior=True)
        self.camNetRight.triangulate()
        self.camNetRight.bundle_adjust(cam_id_list=(0,1,2), unique=False, prior=True)
        
        # put old values back
        for cam_id in range(config["num_cameras"]):
            self.camNetAll[cam_id].points2d = pts2d[cam_id, :].copy()

        self.save_calibration()
        self.set_cameras()


    def save_calibration(self):
        calib_path = f"{self.output_folder}/calib_{self.input_folder.replace('/', '_')}.pkl"
        print("Saving calibration {}".format(calib_path))
        self.camNetAll.save_network(calib_path)


    def save_pose(self, manual_corrections):
        pts2d = np.zeros((7, self.num_images, config["num_joints"], 2), dtype=float)

        for cam in self.camNetAll:
            pts2d[cam.cam_id, :] = cam.points2d.copy()

        # take a copy of unmodified points2d
        pts2d_orig = pts2d.copy()

        # overwrite by manual correction
        count = 0
        for cam_id in range(config["num_cameras"]):
            for img_id in range(self.num_images):
                if img_id in manual_corrections.get(cam_id, {}):
                    pts2d[cam_id, img_id, :] = manual_corrections[cam_id][img_id]
                    count += 1


        if "fly" in config["name"]:
            # some post-processing for body-coxa
            for cam_id in range(len(self.camNetAll.cam_list)):
                for j in range(config["skeleton"].num_joints):
                    if config["skeleton"].camera_see_joint(cam_id, j) and config[
                        "skeleton"
                    ].is_tracked_point(j, config["skeleton"].Tracked.BODY_COXA):
                        pts2d[cam_id, :, j, 0] = np.median(pts2d[cam_id, :, j, 0])
                        pts2d[cam_id, :, j, 1] = np.median(pts2d[cam_id, :, j, 1])

        dict_merge = self.camNetAll.save_network(path=None)
        dict_merge["points2d"] = pts2d

        # ugly hack to temporarly incorporate manual corrections
        c = 0
        for cam_id in range(config["num_cameras"]):
            for img_id in range(self.num_images):
                if img_id in manual_corrections.get(cam_id, {}):
                    pt = manual_corrections[cam_id][img_id]
                    self.camNetAll[cam_id].points2d[img_id, :] = pt
                    c += 1
        print("Replaced points2d with {} manual correction".format(count))

        # do the triangulation if we have the calibration
        if self.camNetLeft.has_calibration() and self.camNetLeft.has_pose():
            self.camNetAll.triangulate()
            pts3d = self.camNetAll.points3d_m

            dict_merge["points3d"] = pts3d
            
        # apply procrustes
        if config["procrustes_apply"]:
            print("Applying Procrustes on 3D Points")
            dict_merge["points3d"] = procrustes_seperate(dict_merge["points3d"])

        # put old values back
        for cam_id in range(config["num_cameras"]):
            self.camNetAll[cam_id].points2d = pts2d_orig[cam_id, :].copy()

        save_path = os.path.join(self.output_folder,"pose_result_{}.pkl".format(self.input_folder.replace("/", "_")))
        pickle.dump(dict_merge, open(save_path,"wb"))
        print(f"Saved the pose at: {save_path}")
