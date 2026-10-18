"""Bounded, read-only process-tree samples while diagnostics are running.
Not an idle/gaming benchmark. No command lines, window titles or screenshots.
"""
from collections import deque
import threading
import time

class Sampler:
    def __init__(self,pid):
        self.pid=pid;self.stop=threading.Event();self.rows=deque(maxlen=4096);self.total=0;self.thread=None
    def start(self):
        if not self.pid:return
        self.thread=threading.Thread(target=self._run,daemon=True,name='NEO-resource-diagnostics');self.thread.start()
    def _run(self):
        import psutil
        try:root=psutil.Process(self.pid);created=root.create_time()
        except psutil.Error:return
        known={}
        while not self.stop.is_set():
            try:
                if not root.is_running() or root.create_time()!=created:break
                live=[root,*root.children(recursive=True)];cpu=0.;rss=0
                for proc in live:
                    try:
                        key=(proc.pid,proc.create_time());cached=known.setdefault(key,proc)
                        cpu+=cached.cpu_percent(None);rss+=cached.memory_info().rss
                    except psutil.Error:pass
                livekeys={(p.pid,p.create_time()) for p in live if p.is_running()}
                known={k:v for k,v in known.items() if k in livekeys}
                self.rows.append({'time':time.time(),'rss_bytes_sum':rss,'cpu_percent_one_core':cpu,'process_count':len(live)})
                self.total+=1
            except psutil.Error:pass
            self.stop.wait(2)
    def finish(self):
        self.stop.set()
        if self.thread:self.thread.join(3)
        return {'status':'SAMPLED' if self.rows else 'NOT_RUN','scope':'NEO process tree during diagnostic workload; sum RSS, CPU relative to one core; first CPU sample is warm-up; no GPU/idle-performance claim',
                'total_samples':self.total,'kept_last_samples':len(self.rows),'samples':list(self.rows)}
