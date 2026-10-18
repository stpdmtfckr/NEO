"""Pixel/frame regressions with deterministic animation clocks and no external I/O."""
from __future__ import annotations
import os,random
from unittest.mock import patch
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QPoint,QCoreApplication,QEvent
from PySide6.QtGui import QImage,QPainter,QColor

@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    for key in ('HOME','LOCALAPPDATA','APPDATA','XDG_DATA_HOME'):monkeypatch.setenv(key,str(tmp_path/key))
    from jarvis.testing.isolation import external_io_blocked
    import socket
    def deny(*a,**k):raise PermissionError('Visual test external I/O blocked')
    with external_io_blocked(),patch.object(socket.socket,'sendto',deny),patch.object(os,'system',deny):yield


def render(widget,dpr=1):
    im=QImage(widget.width()*dpr,widget.height()*dpr,QImage.Format_ARGB32_Premultiplied);im.setDevicePixelRatio(dpr);im.fill(Qt.transparent)
    p=QPainter(im);widget.render(p,QPoint());p.end();return im

@pytest.mark.parametrize('opacity',[0,35,100])
@pytest.mark.parametrize('width,margin,radius,dpr',[(322,8,26,1),(214,5,26,1),(520,13,48,1),(322,8,26,2)])
def test_robot_does_not_erase_any_rounded_frame_pixel(app,opacity,width,margin,radius,dpr):
    from jarvis.ui.main_window import RoundedSurface
    from jarvis.ui.robot_widget import RobotWidget
    surface=RoundedSurface();surface.setAttribute(Qt.WA_TranslucentBackground);surface.resize(width,476)
    surface.set_theme('#0C1017','#57D7FF',radius,background_opacity=opacity)
    baseline=render(surface,dpr)
    robot=RobotWidget(parent=surface);robot.setGeometry(margin,margin,width-2*margin,210);robot.set_background_opacity(opacity);robot._timer.stop()
    actual=render(surface,dpr)
    count=0
    for y in range((radius+5)*dpr):
        for x in list(range((radius+5)*dpr))+list(range((width-radius-5)*dpr,width*dpr)):
            c=baseline.pixelColor(x,y)
            if c.green()>80 and c.blue()>120 and c.green()>c.red()+20:
                assert actual.pixelColor(x,y)==c,(x,y,c.getRgb(),actual.pixelColor(x,y).getRgb())
                count+=1
    assert count>20
    surface.close();surface.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.fixture
def robot(app):
    from jarvis.ui.robot_widget import RobotWidget
    random.seed(184);r=RobotWidget();r.resize(520,340);r._timer.stop();r.set_accessories(coffee=True,cigarette=True)
    r._acc177['coffee']['next']=0;r._acc177['cigarette']['next']=1000;r._wisp_next181=1000
    render(r)
    yield r
    r.close();r.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def step(r,seconds,paint=False):
    for _ in range(round(seconds*60)):
        r._elapsed+=1/60;r._advance_accessories177(1/60);r._smooth_face(1/60)
        if paint:render(r)


def test_combined_sequence_exhales_before_sip_and_holds_cigarette_aside(robot):
    phases=[];first_exhale=first_sip=None
    # NEO 185 (ТЗ): slower cigarette removal/return and a longer hold -> a longer break.
    for i in range(1080):
        step(robot,1/60,paint=(i%12==0))
        if not phases or phases[-1]!=robot._combo_phase184:phases.append(robot._combo_phase184)
        if phases==['smoke','sip','return','']:break   # one full break; later wisps are a separate effect
        if robot._elapsed<robot._exhale_until177:
            first_exhale=first_exhale or robot._elapsed
            assert robot._accessory_lift177('coffee')==0
            assert robot._accessory_lift177('cigarette')==1
        if robot._accessory_lift177('coffee')>0:
            first_sip=first_sip or robot._elapsed
            assert robot._elapsed>robot._exhale_until177
            assert robot._accessory_lift177('cigarette')==1
    assert phases==['smoke','sip','return','']
    assert first_exhale<first_sip
    assert robot._exhale_ring_count184==3
    assert robot._accessory_lift177('cigarette')==0
    assert len(robot._particles177)<=150

@pytest.mark.parametrize('interrupt_time',[.45,2.2,4.4,5.6])
@pytest.mark.parametrize('state',['speaking','listening','sleeping'])
def test_combined_sequence_interrupts_without_snapping_or_sticking(robot,interrupt_time,state):
    step(robot,interrupt_time)
    old=[robot._accessory_lift177(k) for k in ('coffee','cigarette')]
    robot._state=state;step(robot,1/60)
    now=[robot._accessory_lift177(k) for k in ('coffee','cigarette')]
    assert all(abs(a-b)<.08 for a,b in zip(old,now)),(old,now)
    step(robot,3)
    assert robot._combo_phase184==''
    assert robot._accessory_lift177('coffee')==0
    assert robot._accessory_lift177('cigarette')==0
    assert robot._elapsed>=robot._exhale_until177


def test_disabling_accessories_drops_only_their_particles(robot):
    step(robot,2,paint=True);assert robot._combo_phase184=='smoke'
    robot.set_accessories(coffee=True,cigarette=False)
    assert robot._combo_phase184=='' and robot._ember177 is None
    assert all(p[10]==0 for p in robot._particles177)
    robot.set_accessories(coffee=False,cigarette=False)
    assert robot._particles177==[]


def test_ambient_cigarette_smoke_is_sparse_and_low_opacity(robot):
    robot.set_accessories(coffee=False,cigarette=True);robot._acc177['cigarette']['next']=1000
    robot._ember177=(100.,200.,1.);step(robot,5)
    ambient=[p for p in robot._particles177 if p[10]==1]
    # NEO 185 (ТЗ): smoke more visible but moderate, rising slower and living longer.
    assert 1<=len(ambient)<=14
    assert max(p[8] for p in ambient)<=70
    assert max(p[7] for p in ambient)<=9
    assert min(p[1] for p in ambient)>160


def test_smoke_ring_is_round_with_clear_center(robot):
    robot._particles177=[[80.,80.,0.,0.,1.,4.,4.,24.,220.,0.,3.,1.]]
    im=QImage(160,160,QImage.Format_ARGB32_Premultiplied);im.fill(Qt.transparent);p=QPainter(im);robot._draw_particles177(p);p.end()
    pixels=[(x,y) for y in range(160) for x in range(160) if im.pixelColor(x,y).alpha()>20]
    width=max(x for x,y in pixels)-min(x for x,y in pixels);height=max(y for x,y in pixels)-min(y for x,y in pixels)
    assert .68<height/width<.95
    assert im.pixelColor(80,80).alpha()==0
