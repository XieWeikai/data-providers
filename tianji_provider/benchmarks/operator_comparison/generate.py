"""Generate deterministic ROS 2 CDR and H.264 MCAPs; refuse an existing destination."""
from fractions import Fraction
import argparse
import math
import json
from pathlib import Path

import av
import numpy as np
import yaml
from mcap_ros2.writer import Writer

HEADER = {"std_msgs/Header": "builtin_interfaces/Time stamp\nstring frame_id",
          "builtin_interfaces/Time": "int32 sec\nuint32 nanosec"}
XYZ = "float64 x\nfloat64 y\nfloat64 z"
DEFS = {
    "JointState": ("sensor_msgs", "std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort", HEADER),
    "JointcmdArm": ("marvin_msgs", "float64[] positions", {}),
    "String": ("std_msgs", "string data", {}),
    "Float32": ("std_msgs", "float32 data", {}),
    "Float32MultiArray": ("std_msgs", "std_msgs/MultiArrayLayout layout\nfloat32[] data", {"std_msgs/MultiArrayLayout":"std_msgs/MultiArrayDimension[] dim\nuint32 data_offset","std_msgs/MultiArrayDimension":"string label\nuint32 size\nuint32 stride"}),
    "PoseStamped": ("geometry_msgs","std_msgs/Header header\ngeometry_msgs/Pose pose",{**HEADER,"geometry_msgs/Pose":"geometry_msgs/Point position\ngeometry_msgs/Quaternion orientation","geometry_msgs/Point":XYZ,"geometry_msgs/Quaternion":XYZ+'\nfloat64 w'}),
    "WrenchStamped": ("geometry_msgs","std_msgs/Header header\ngeometry_msgs/Wrench wrench",{**HEADER,"geometry_msgs/Wrench":"geometry_msgs/Vector3 force\ngeometry_msgs/Vector3 torque","geometry_msgs/Vector3":XYZ}),
    "CompressedImage": ("sensor_msgs","std_msgs/Header header\nstring format\nuint8[] data",HEADER),
}
def register(writer, kind):
    pkg, root, children = DEFS[kind]
    schema = root+''.join('\n'+'='*80+'\nMSG: '+name+'\n'+fields for name,fields in children.items())+'\n'
    return writer.register_msgdef(f'{pkg}/msg/{kind}',schema)

def header(t, frame):
    return {"stamp":{"sec":t//10**9,"nanosec":t%10**9},"frame_id":frame}

def xyz(x,y,z):
    return dict(x=x,y=y,z=z)

def manifest(path, origin):
    (path/'metadata.yaml').write_text(yaml.safe_dump({"rosbag2_bagfile_information":{
        "relative_file_paths":["part.mcap"],"starting_time":{"nanoseconds_since_epoch":origin}}}))

def generate(root, index, seconds, width, height):
    root.mkdir()
    origin = 1750000000000000000 + index * 100_000_000_000
    data = root/'data';data.mkdir()
    with (data/'part.mcap').open('wb') as stream:
        w=Writer(stream); defs={k:register(w,k) for k in DEFS if k!='CompressedImage'}
        def emit(topic,kind,value,t):
            w.write_message(topic, defs[kind], value, log_time=t, publish_time=t)
        for i in range(seconds*1000+1):
            t=origin+i*1_000_000
            # Quiet endpoints, continuous movement and a strictly interior pause.
            phase=min(max((i-430)/(seconds*1000-900),0),1)
            x=.12*(1-math.cos(phase*math.pi)) if not .45<phase<.55 else .12
            if i%200 == 0:
                mode = 3 if seconds*350 <= i < seconds*650 else 1
                status = dict(stamp_ns=t, source='teleop' if mode==1 else 'user',arm_mode=mode,
                              gripper_mode=mode,output_enabled=True,switching=False,fault='')
                emit('/tj/dagger/status','String',dict(data=json.dumps(status)),t)
            if i%4==0:
                names=[f'Joint{j}_{s}' for s in ('L','R') for j in range(1,8)]
                q=[x*(1+j/20)+index*.001 for j in range(14)]
                emit('/tj/joint_states','JointState',dict(header=header(t,'base_link'),name=names[::-1],position=q[::-1],velocity=[0.0]*14,effort=[]),t)
            for side,arm,long in [('L','A','left'),('R','B','right')]:
                bias=0 if side=='L' else .03
                if i%20==0:
                    if i>=600 or arm=='A':
                        emit('/tj/control/joint_cmd_'+arm,'JointcmdArm',{'positions':[x+bias+j*.01 for j in range(7)]},t)
                    emit('/control/gripperValue'+side,'Float32',{'data':.25+x+bias},t)
                if i%10==0:
                    emit('/info/gripper_feedback_'+side,'Float32MultiArray',{'layout':{'dim':[],'data_offset':0},'data':[1.6*(.25+x+bias),0.,0.,0.,0.]},t)
                if i%8==0:
                    sign=1 if (i//80)%2==0 else -1
                    emit('/tj/info/eef_'+long,'PoseStamped',dict(header=header(t,'base_link'),pose=dict(position=xyz(x,bias,-x),orientation=dict(x=0.,y=0.,z=sign*math.sin(x),w=sign*math.cos(x)))),t)
                if i%5==0:
                    emit('/tj/info/wrench_'+long,'WrenchStamped',dict(header=header(t,'flange_'+side),wrench=dict(force=xyz(x*10,math.sin(i/17),bias),torque=xyz(i/3000,bias,x))),t)
        w.finish()
    manifest(data, origin)
    video=root/'video'/'mosaic';video.mkdir(parents=True)
    with (video/'part.mcap').open('wb') as stream:
        w=Writer(stream);schema=register(w,'CompressedImage')
        encoder=av.CodecContext.create('libx264','w');encoder.width=width;encoder.height=height
        encoder.pix_fmt='yuv420p';encoder.time_base=Fraction(1,30);encoder.framerate=Fraction(30,1);encoder.thread_count=1
        encoder.options={'preset':'veryfast','tune':'zerolatency','crf':'18','bf':'0'};encoder.open()
        y,x=np.indices((height,width),dtype=np.int32); count=seconds*30+1
        for i in range(count):
            rgb=np.stack(((x+i*3)%256,(y+i*7+index*10)%256,((x//9)^(y//7)^i)%256),axis=2).astype('uint8')
            frame=av.VideoFrame.from_ndarray(rgb,format='rgb24');frame.pts=i
            packet,=encoder.encode(frame)
            t=origin+(i*10**9+15)//30
            w.write_message('/recorder/quad_tile/compressed_undistorted',schema,{'header':header(t,'mosaic'),'format':'h264','data':bytes(packet)},log_time=t,publish_time=t,sequence=i)
        assert not encoder.encode(None)
        w.finish()
    manifest(video,origin)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('destination',type=Path)
    p.add_argument('--bags',type=int,default=2);p.add_argument('--seconds',type=int,default=6)
    p.add_argument('--width',type=int,default=1920);p.add_argument('--height',type=int,default=1080)
    args=p.parse_args();args.destination.mkdir()
    for i in range(args.bags):
        generate(args.destination/f'generated-{i:03d}',i,args.seconds,args.width,args.height)
