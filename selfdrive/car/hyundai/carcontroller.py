from cereal import car, log
from common.realtime import DT_CTRL
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfa_mfa, create_mdps12
from selfdrive.car.hyundai.values import Buttons, SteerLimitParams, CAR
from opendbc.can.packer import CANPacker
from selfdrive.config import Conversions as CV

import common.log as trace1
import common.CTime1000 as tm

VisualAlert = car.CarControl.HUDControl.VisualAlert
LaneChangeState = log.PathPlan.LaneChangeState



class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP    
    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.packer = CANPacker(dbc_name)
    self.steer_rate_limited = False

    # resume
    self.resume_cnt = 0    
    self.last_resume_frame = 0
    self.last_lead_distance = 0

    self.lkas11_cnt = 0

    # hud
    self.hud_timer_left = 0
    self.hud_timer_right = 0

    self.enable_time = 0
    self.steer_torque_over_timer = 0
    self.steer_torque_ratio =  1

    self.timer1 = tm.CTime1000("time") 

  def limit_ctrl(self, value, limit, offset ):
      p_limit = offset + limit
      m_limit = offset - limit
      if value > p_limit:
          value = p_limit
      elif  value < m_limit:
          value = m_limit
      return value


  def process_hud_alert(self, enabled, c ):
    visual_alert = c.hudControl.visualAlert
    left_lane = c.hudControl.leftLaneVisible
    right_lane = c.hudControl.rightLaneVisible

    sys_warning = (visual_alert == VisualAlert.steerRequired)

    if left_lane:
      self.hud_timer_left = 100

    if right_lane:
      self.hud_timer_right = 100

    if self.hud_timer_left:
      self.hud_timer_left -= 1
 
    if self.hud_timer_right:
      self.hud_timer_right -= 1


    # initialize to no line visible
    sys_state = 1
    if self.hud_timer_left and self.hud_timer_right or sys_warning:  # HUD alert only display when LKAS status is active
      if (self.steer_torque_ratio > 0.8) and (enabled or sys_warning):
        sys_state = 3
      else:
        sys_state = 4
    elif self.hud_timer_left:
      sys_state = 5
    elif self.hud_timer_right:
      sys_state = 6

    return sys_warning, sys_state

  def steerParams_torque(self, CS, abs_angle_steers, path_plan ):
    param = SteerLimitParams()
    v_ego_kph = CS.out.vEgo * CV.MS_TO_KPH

    self.enable_time = self.timer1.sampleTime()
    if self.enable_time < 50:
      self.steer_torque_over_timer = 0
      self.steer_torque_ratio = 1
      return param

    sec_pval = 0.5  # 0.5 sec 운전자 => 오파  (sec)
    sec_mval = 5.0  # 오파 => 운전자.  (sec)
    # streer over check
    if path_plan.laneChangeState != LaneChangeState.off:
      self.steer_torque_over_timer = 0
      sec_mval = 15.0
    elif CS.out.leftBlinker or CS.out.rightBlinker:
      sec_mval = 2.0  # 오파 => 운전자.

    if v_ego_kph > 5 and abs( CS.out.steeringTorque ) > 150:  #사용자 핸들 토크
      self.steer_torque_over_timer = 50
    elif self.steer_torque_over_timer:
      self.steer_torque_over_timer -= 1

    ratio_pval = 1/(100*sec_pval)
    ratio_mval = 1/(100*sec_mval)

    if self.steer_torque_over_timer:
      self.steer_torque_ratio -= ratio_mval
    else:
      self.steer_torque_ratio += ratio_pval

    if self.steer_torque_ratio < 0:
      self.steer_torque_ratio = 0
    elif self.steer_torque_ratio > 1:
      self.steer_torque_ratio = 1      

    return  param

  def update(self, c, CS, frame, sm ):
    enabled = c.enabled
    actuators = c.actuators
    pcm_cancel_cmd = c.cruiseControl.cancel
    abs_angle_steers =  abs(actuators.steerAngle)

    path_plan = sm['pathPlan']    

    # Steering Torque
    param = self.steerParams_torque( CS, abs_angle_steers, path_plan ) 
    new_steer = actuators.steer * param.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, param)
    self.steer_rate_limited = new_steer != apply_steer


    apply_steer_limit = param.STEER_MAX
    if self.steer_torque_ratio < 1:
      apply_steer_limit = int(self.steer_torque_ratio * param.STEER_MAX)
      apply_steer = self.limit_ctrl( apply_steer, apply_steer_limit, 0 )

    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    lkas_active = enabled and abs(CS.out.steeringAngle) < 90.

    # fix for Genesis hard fault at low speed
    if CS.out.vEgo < 16.7 and self.car_fingerprint == CAR.HYUNDAI_GENESIS:
      lkas_active = False

    if not lkas_active:
      apply_steer = 0

    steer_req = 1 if apply_steer else 0      

    self.apply_steer_last = apply_steer

    sys_warning, sys_state = self.process_hud_alert( lkas_active, c )


    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"]
    self.lkas11_cnt = (self.lkas11_cnt + 1) % 0x10

    can_sends = []
    can_sends.append(create_lkas11(self.packer, self.lkas11_cnt, self.car_fingerprint, apply_steer, steer_req,
                                   CS.lkas11, sys_warning, sys_state, c ))

    if  self.car_fingerprint in [CAR.GRANDEUR_HEV_19]:
      can_sends.append(create_mdps12(self.packer, frame, CS.mdps12))                                   


    str_log1 = 'torg:{:>3.0f}/{:>3.0f}'.format( apply_steer, new_steer )
    str_log2 = 'max={:>3.0f} tm={:>5.1f} '.format( apply_steer_limit, self.timer1.sampleTime()  )
    trace1.printf( '{} {}'.format( str_log1, str_log2 ) )

    lfa_usm = CS.lfahda["LFA_USM"]
    lfa_warn= CS.lfahda["LFA_SysWarning"]
    lfa_active = CS.lfahda["ACTIVE2"]

    hda_usm = CS.lfahda["HDA_USM"]
    hda_active = CS.lfahda["ACTIVE"]
    str_log1 = 'hda={:.0f},{:.0f}'.format( hda_usm, hda_active )
    str_log2 = 'lfa={:.0f},{:.0f},{:.0f}'.format( lfa_usm, lfa_warn, lfa_active  )
    trace1.printf2( '{} {}'.format( str_log1, str_log2 ) )


    if pcm_cancel_cmd and self.CP.openpilotLongitudinalControl:
      can_sends.append(create_clu11(self.packer, frame, CS.clu11, Buttons.CANCEL))
    elif CS.out.cruiseState.standstill:
      # run only first time when the car stopped
      if self.last_lead_distance == 0:
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance != self.last_lead_distance and (frame - self.last_resume_frame) > 5:
        can_sends.append(create_clu11(self.packer, self.resume_cnt, CS.clu11, Buttons.RES_ACCEL))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = frame
    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0


    #elif CS.out.cruiseState.standstill:
    #  # SCC won't resume anyway when the lead distace is less than 3.7m
    #  # send resume at a max freq of 5Hz
    #  if CS.lead_distance > 3.7 and (frame - self.last_resume_frame)*DT_CTRL > 0.2:
    #    can_sends.append(create_clu11(self.packer, frame, CS.clu11, Buttons.RES_ACCEL))
    #    self.last_resume_frame = frame


    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.car_fingerprint in [CAR.SONATA, CAR.PALISADE, CAR.IONIQ, CAR.GRANDEUR_HEV_19, CAR.GRANDEUR_HEV_20]:
      can_sends.append(create_lfa_mfa(self.packer, frame, enabled))

    return can_sends
