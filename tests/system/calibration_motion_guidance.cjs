'use strict';
const assert=require('node:assert/strict');
const guidance=require('../../console/web/calibration-motion');
const id='capture-1', op=id+'/step/2';
const goal={tool:'verify_sensor_extrinsics',field:'verified',equals:true};
const assess={status:'SUCCEEDED',feedback:{goal,required_motion_tools:['physical-provider']}};
const move={status:'PENDING',operation_id:op,physical_pin:{class:'physical',reservation_id:op,verifier_id:'independent'},
  physical_cap:{effect:'actuation'}};
const task={id,state:'WAITING',steps:[assess,move]};
const station={task_id:id,state:'CALIBRATING',phase:'MORE_VIEWS'};
const moving={fresh:true,data:{v:0,w:.12}};
assert.match(guidance(task,station,moving).title,/正在自主换位/);
assert.match(guidance(task,station,{...moving,fresh:false}).title,/等待底盘换位回执/);
assert.equal(guidance({...task,steps:[{feedback:{goal}}]},station,moving),null);
assert.equal(guidance(task,{...station,task_id:'other',active_operation:'other/step/1'},moving),null);
move.status='SUCCEEDED';
move.evidence={outcome:'arrived',verifier_id:'independent',verification:{operation_id:'wrong'}};
assert.doesNotMatch(guidance(task,station,moving).title,/已到位/);
move.evidence.verification={operation_id:op,position_error_m:.008,heading_error_rad:.01};
assert.match(guidance(task,station,moving).title,/已到位/);
assert.match(guidance(task,{...station,phase:'RECORDING'},moving).title,/正在录制稳态样本/);
assert.equal(guidance({...task,state:'SUCCEEDED'},{...station,state:'VERIFIED'},moving),null);
const placement={tool:'inspect_calibration_view',status:'RUNNING',operation_id:id+'/step/1',
  feedback:{goal,placement_required:true}};
const setup={...task,steps:[placement]};
assert.match(guidance(setup,station,moving).title,/立在车头正前方/);
placement.status='SUCCEEDED';
placement.evidence={front_placement_confirmed:true};
placement.operation_id='other/step/1';
assert.match(guidance(setup,station,moving).title,/立在车头正前方/);
placement.operation_id=id+'/step/1';
assert.match(guidance(setup,station,moving).title,/已确认正面板位/);
const verify={tool:'verify_sensor_extrinsics',operation_id:id+'/step/1',status:'SUCCEEDED',evidence:{
  verified:true,revision:'one',transforms:{front:{parent_frame:'rgb',child_frame:'front',translation_m:[0,.2,.045],
    rotation_xyzw:[0,0,0,1],validation:{max_hole_error_m:.015,max_angle_deg:.8}}}}};
const verified={...station,state:'VERIFIED',revision:'one'};
assert.ok(guidance.resultRows({...task,steps:[verify]},verified).some(([,v])=>v==='15.0 毫米'));
assert.deepEqual(guidance.resultRows({...task,steps:[verify]},{...verified,revision:'another'}),[]);
assert.deepEqual(guidance.resultRows({...task,steps:[verify]},{...verified,task_id:'another'}),[]);
console.log('PASS runtime physical calibration guidance');
