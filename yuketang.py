import asyncio
import websockets
import json
import requests
import os
import time
import re
import ast
from datetime import datetime
from weakref import WeakValueDictionary
from util import *
from send import *
from llm import *
from random import *

current_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_dir)

with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

timeout = config['yuketang']['timeout']
users = config['yuketang']['users']

_FETCH_LOCKS_1 = WeakValueDictionary()
_FETCH_LOCKS_2 = WeakValueDictionary()

def _get_fetch_lock_1(lessonId):
    key = str(lessonId)
    lock = _FETCH_LOCKS_1.get(key)
    if lock is None:
        new_lock = asyncio.Lock()
        lock = _FETCH_LOCKS_1.setdefault(key, new_lock)
    return lock

def _get_fetch_lock_2(lessonId):
    key = str(lessonId)
    lock = _FETCH_LOCKS_2.get(key)
    if lock is None:
        new_lock = asyncio.Lock()
        lock = _FETCH_LOCKS_2.setdefault(key, new_lock)
    return lock

class yuketang:
    def __init__(self, yt_config):
        self.name = yt_config['name']
        self.domain = yt_config['domain']
        self.cookie = ''
        self.cookieTime = ''
        self.lessonIdNewList = []
        self.lessonIdDict = {}
        self.classroomCodeList = yt_config['classroomCodeList']
        self.classroomWhiteList = yt_config['classroomWhiteList']
        self.classroomBlackList = yt_config['classroomBlackList']
        self.classroomStartTimeDict = yt_config['classroomStartTimeDict']
        self.llm = yt_config['llm']
        self.an = yt_config['an']
        self.ppt = yt_config['ppt']
        self.si = yt_config['si']
        self.msgmgr = SendManager(f"[{self.name}]\n", yt_config['services'])

    async def get_cookie(self):
        flag = 0
        def read_cookie():
            with open(f"cookie_{self.name}.txt", "r") as f:
                lines = f.readlines()
            self.cookie = lines[0].strip()
            if len(lines) >= 2:
                self.cookieTime = convert_date(int(lines[1].strip()))
            else:
                self.cookieTime = ''
        while True:
            if not os.path.exists(f"cookie_{self.name}.txt"):
                flag = 1
                await asyncio.to_thread(self.msgmgr.sendMsg, "正在第一次获取登录cookie, 请微信扫码")
                await self.ws_controller(self.ws_login, retries=1000, delay=1)
            if not self.cookie:
                flag = 1
                read_cookie()
            if self.cookieTime and not check_time(self.cookieTime, 0):
                flag = 1
                await asyncio.to_thread(self.msgmgr.sendMsg, "cookie已失效, 请重新扫码")
                await self.ws_controller(self.ws_login, retries=1000, delay=1)
                read_cookie()
                continue
            elif self.cookieTime and (not check_time(self.cookieTime, 2880) and datetime.now().minute < 5 or not check_time(self.cookieTime, 120)):
                flag = 1
                await asyncio.to_thread(self.msgmgr.sendMsg, f"cookie有效至{self.cookieTime}, 即将失效, 请重新扫码")
                await self.ws_controller(self.ws_login, retries=0, delay=1)
                read_cookie()
                continue
            code = self.check_cookie()
            if code == 1:
                flag = 1
                await asyncio.to_thread(self.msgmgr.sendMsg, "cookie已失效, 请重新扫码")
                await self.ws_controller(self.ws_login, retries=1000, delay=1)
                read_cookie()
            elif code == 0:
                if self.cookieTime and flag == 1 and check_time(self.cookieTime, 2880):
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"cookie有效至{self.cookieTime}")
                elif self.cookieTime and flag == 1:
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"cookie有效至{self.cookieTime}, 即将失效, 下个小时初注意扫码")
                elif flag == 1:
                    await asyncio.to_thread(self.msgmgr.sendMsg, "cookie有效, 有效期未知")
                break

    def web_login(self, UserID, Auth):
        url = f"https://{self.domain}/pc/web_login"
        data = {
            "UserID": UserID,
            "Auth": Auth
        }
        headers = {
            "referer" : f"https://{self.domain}/web?next=/v2/web/index&type=3",
            "User-Agent" : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Content-Type" : "application/json"
        }
        try:
            res = requests.post(url=url, headers=headers, json=data, timeout=timeout)
        except Exception as e:
            print(f"登录失败: {e}")
            return
        cookies = res.cookies
        self.cookie = ""
        for k, v in cookies.items():
            self.cookie += f'{k}={v};'
        date = cookie_date(res)
        if date:
            content = f'{self.cookie}\n{date}'
            self.cookieTime = convert_date(int(date))
        else:
            content = self.cookie
        with open(f"cookie_{self.name}.txt", "w") as f:
            f.write(content)

    def check_cookie(self):
        info = self.get_basicinfo()
        if not info:
            return 2
        if info.get("code") == 0:
            return 0
        return 1
    
    def set_authorization(self, res, lessonId):
        if res.headers.get("Set-Auth") is not None:
            self.lessonIdDict[lessonId]['Authorization'] = "Bearer " + res.headers.get("Set-Auth")

    def join_classroom(self):
        classroomCodeList_del = []
        for classroomCode in self.classroomCodeList:
            if len(classroomCode) == 5:
                data = {"source": 14, "inviteCode": classroomCode}
                url = f"https://{self.domain}/api/v3/lesson/notkn/checkin"
                headers = {
                    "cookie" : self.cookie,
                    "x-csrftoken" : self.cookie.split("csrftoken=")[1].split(";")[0],
                    "Content-Type" : "application/json"
                }
                try:
                    res = requests.post(url=url, headers=headers, json=data, timeout=timeout)
                except:
                    continue
                if res.json().get("msg", "") == "OK":
                    self.msgmgr.sendMsg(f"课堂暗号{classroomCode}使用成功, 正在上课")
                    classroomCodeList_del.append(classroomCode)
                elif res.json().get("msg", "") == "LESSON_END_JOIN":
                    self.msgmgr.sendMsg(f"课堂暗号{classroomCode}使用成功, 课堂已结束")
                    classroomCodeList_del.append(classroomCode)
                elif res.json().get("msg", "") == "LESSON_INVITE_CODE_TIMEOUT":
                    self.msgmgr.sendMsg(f"课堂暗号{classroomCode}不存在")
                    classroomCodeList_del.append(classroomCode)
                # else:
                #    self.msgmgr.sendMsg(f"课堂暗号{classroomCode}使用失败")
            elif len(classroomCode) == 6:
                data = {"id": classroomCode}
                url = f"https://{self.domain}/v/course_meta/join_classroom"
                headers = {
                    "cookie": self.cookie,
                    "x-csrftoken": self.cookie.split("csrftoken=")[1].split(";")[0],
                    "Content-Type": "application/json"
                }
                try:
                    res = requests.post(url=url, headers=headers, json=data, timeout=timeout)
                except:
                    continue
                if res.json().get("success", False) == True:
                    self.msgmgr.sendMsg(f"班级邀请码{classroomCode}使用成功")
                    classroomCodeList_del.append(classroomCode)
                elif "班级邀请码或课堂暗号不存在" in res.json().get("msg", ""):
                    self.msgmgr.sendMsg(f"班级邀请码{classroomCode}不存在")
                    classroomCodeList_del.append(classroomCode)
                # else:
                #    self.msgmgr.sendMsg(f"班级邀请码{classroomCode}使用失败")
            else:
                self.msgmgr.sendMsg(f"班级邀请码/课堂暗号{classroomCode}格式错误")
                classroomCodeList_del.append(classroomCode)
                continue
        self.classroomCodeList = list(set(self.classroomCodeList) - set(classroomCodeList_del))

    def get_basicinfo(self):
        url = f"https://{self.domain}/api/v3/user/basic-info"
        headers = {
            "referer": f"https://{self.domain}/web?next=/v2/web/index&type=3",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie": self.cookie
        }
        try:
            res = requests.get(url=url, headers=headers, timeout=timeout).json()
            return res
        except:
            return {}

    def lesson_info(self, lessonId):
        url = f"https://{self.domain}/api/v3/lesson/basic-info"
        headers = {
            "referer": f"https://{self.domain}/lesson/fullscreen/v3/{lessonId}?source=5",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie": self.cookie,
            "Authorization": self.lessonIdDict[lessonId]['Authorization']
        }
        try:
            res = requests.get(url=url, headers=headers, timeout=timeout)
        except:
            return
        self.set_authorization(res, lessonId)
        classroomName = self.lessonIdDict[lessonId]['classroomName']
        self.lessonIdDict[lessonId]['header'] = f"PPT编号: {self.lessonIdDict[lessonId].get('presentation', '待获取')}\n课程: {classroomName}\n"
        try:
            self.lessonIdDict[lessonId]['title'] = res.json()['data']['title']
            self.lessonIdDict[lessonId]['header'] += f"标题: {self.lessonIdDict[lessonId]['title']}\n教师: {res.json()['data']['teacher']['name']}\n开始时间: {convert_date(res.json()['data']['startTime'])}"
        except:
            self.lessonIdDict[lessonId]['title'] = '未知标题'
            self.lessonIdDict[lessonId]['header'] += f"标题: 获取失败\n教师: 获取失败\n开始时间: 获取失败"

    def get_lesson(self):
        url = f"https://{self.domain}/api/v3/classroom/on-lesson-upcoming-exam"
        headers = {
            "referer": f"https://{self.domain}/web?next=/v2/web/index&type=3",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie": self.cookie
        }
        try:
            online_data = requests.get(url=url, headers=headers, timeout=timeout).json()
        except:
            return (False, [])
        try:
            to_close_ids = []
            self.lessonIdNewList = []
            if online_data['data']['onLessonClassrooms'] == []:
                to_close_ids = list(self.lessonIdDict.keys())
                return (False, to_close_ids)
            
            for item in online_data['data']['onLessonClassrooms']:
                if (self.classroomWhiteList and item['classroomName'] not in self.classroomWhiteList) or item['classroomName'] in self.classroomBlackList or (self.classroomStartTimeDict and item['classroomName'] in self.classroomStartTimeDict and not check_time2(self.classroomStartTimeDict[item['classroomName']])):
                    continue
                lessonId = item['lessonId']
                if lessonId not in self.lessonIdDict:
                    self.lessonIdNewList.append(lessonId)
                    self.lessonIdDict[lessonId] = {}
                    self.lessonIdDict[lessonId]['startTime'] = time.time()
                    self.lessonIdDict[lessonId]['classroomName'] = item['classroomName']
                self.lessonIdDict[lessonId]['active'] = '1'

            to_delete = [lessonId for lessonId, details in self.lessonIdDict.items() if details.get('active', '0') != '1']
            to_close_ids.extend(to_delete)

            for lessonId in self.lessonIdDict:
                self.lessonIdDict[lessonId]['active'] = '0'

            if self.lessonIdNewList:
                return (True, to_close_ids)
            else:
                return (False, to_close_ids)
        except:
            return (False, [])

    def lesson_checkin(self):
        for lessonId in self.lessonIdNewList:
            url = f"https://{self.domain}/api/v3/lesson/checkin"
            headers = {
                "referer": f"https://{self.domain}/lesson/fullscreen/v3/{lessonId}?source=5",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
                "Content-Type": "application/json; charset=utf-8",
                "cookie": self.cookie
            }
            data = {
                "source": 5,
                "lessonId": lessonId
            }
            try:
                res = requests.post(url=url, headers=headers, json=data, timeout=timeout)
            except:
                return
            self.set_authorization(res, lessonId)
            self.lesson_info(lessonId)
            try:
                self.lessonIdDict[lessonId]['Auth'] = res.json()['data']['lessonToken']
                self.lessonIdDict[lessonId]['userid'] = res.json()['data']['identityId']
            except:
                self.lessonIdDict[lessonId]['Auth'] = ''
                self.lessonIdDict[lessonId]['userid'] = ''
            checkin_status = res.json()['msg']
            if checkin_status == 'OK':
                self.msgmgr.sendMsg(f"{self.lessonIdDict[lessonId]['header']}\n消息: 签到成功")
            elif checkin_status == 'LESSON_END':
                self.msgmgr.sendMsg(f"{self.lessonIdDict[lessonId]['header']}\n消息: 课程已结束")
            else:
                self.msgmgr.sendMsg(f"{self.lessonIdDict[lessonId]['header']}\n消息: 签到失败")

    async def fetch_presentation(self, lessonId):
        await asyncio.sleep(1)
        async with _get_fetch_lock_1(lessonId):  # 同一 lessonId 串行，跨 lessonId 并行
            if lessonId not in self.lessonIdDict: return
            lesson = self.lessonIdDict[lessonId]
            ppt_id = lesson['presentation']
            if os.path.exists(ppt_id) and os.path.exists(os.path.join(ppt_id, "ppt.json")):
                with open(os.path.join(ppt_id, "ppt.json"), "r", encoding="utf-8") as f:
                    info = json.load(f)
            else:
                url = f"https://{self.domain}/api/v3/lesson/presentation/fetch?presentation_id={ppt_id}"
                headers = {
                    "referer": f"https://{self.domain}/lesson/fullscreen/v3/{lessonId}?source=5",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
                    "cookie": self.cookie,
                    "Authorization": lesson['Authorization']
                }
                res = await asyncio.to_thread(requests.get, url, headers=headers, timeout=timeout)
                self.set_authorization(res, lessonId)
                info = res.json()

            slides = info['data']['slides']
            problems = {}
            lesson['problems'] = {}
            lesson['covers'] = [slide['index'] for slide in slides if slide.get('cover') is not None]
            for slide in slides:
                if slide.get("problem") is not None:
                    lesson['problems'][slide['id']] = slide['problem']
                    lesson['problems'][slide['id']]['index'] = slide['index']
                    problems[slide['index']] = {"problemType": int(slide['problem']['problemType']), "option_keys": [opt['key'] for opt in slide['problem'].get('options', [])], "option_values": [opt['value'] for opt in slide['problem'].get('options', [])], "num_blanks": len(slide['problem'].get('blanks', [])), "pollingCount": int(slide['problem'].get('pollingCount', 1)), "score": int(slide['problem'].get('score', 0))}
                    if slide['problem']['body'] == '':
                        shapes = slide.get('shapes', [])
                        if shapes:
                            min_left_item = min(shapes, key=lambda item: item.get('Left', 9999999))
                            left_val = min_left_item.get('Left', 9999999)
                            if left_val != 9999999 and min_left_item.get('Text') is not None:
                                lesson['problems'][slide['id']]['body'] = min_left_item['Text'] or '未知问题'
                            else:
                                lesson['problems'][slide['id']]['body'] = '未知问题'
                        else:
                            lesson['problems'][slide['id']]['body'] = '未知问题'
                    problems[slide['index']]['body'] = lesson['problems'][slide['id']]['body'] if lesson['problems'][slide['id']]['body'] != '未知问题' else ''
            await asyncio.to_thread(self.msgmgr.sendMsg, f"{lesson['header']}\n{format_json_to_text(lesson['problems'], lesson.get('unlockedproblem', []))}")
            if self.lessonIdDict.get(lessonId, {}).get('presentation', 0) != ppt_id: return
            self.lessonIdDict[lessonId]['problems'] = lesson['problems']
            self.lessonIdDict[lessonId]['covers'] = lesson['covers']

        async with _get_fetch_lock_2(lessonId):  # 同一 lessonId 串行，跨 lessonId 并行
            if self.lessonIdDict.get(lessonId, {}).get('presentation', 0) != ppt_id: return
            output_pdf_path = os.path.join(ppt_id, lesson['classroomName'].strip() + "-" + lesson['title'].strip() + ".pdf")
            if not os.path.exists(ppt_id) or not os.path.exists(output_pdf_path):
                await asyncio.to_thread(clear_folder, ppt_id)
                with open(os.path.join(ppt_id, "ppt.json"), "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False, indent=4)
                await asyncio.to_thread(download_images_to_folder, slides, ppt_id)
                await asyncio.to_thread(images_to_pdf, ppt_id, output_pdf_path)

                if self.ppt:
                    if os.path.exists(output_pdf_path):
                        try:
                            await asyncio.to_thread(self.msgmgr.sendFile, output_pdf_path)
                        except:
                            await asyncio.to_thread(self.msgmgr.sendMsg, f"{lesson['header']}\n消息: PPT推送失败")
                    else:
                        await asyncio.to_thread(self.msgmgr.sendMsg, f"{lesson['header']}\n消息: 没有PPT")

            problems_keys = [int(k) for k in problems.keys()]
            if not os.path.exists(os.path.join(ppt_id, "problems.txt")):
                if problems:
                    await asyncio.to_thread(concat_vertical_cv, ppt_id, 0, 100)
                    await asyncio.to_thread(concat_vertical_cv, ppt_id, 1, 100)
                    await asyncio.to_thread(concat_vertical_cv, ppt_id, 2, 100)
                    await asyncio.to_thread(concat_vertical_cv, ppt_id, 3, 100, problems_keys)
                    await asyncio.to_thread(concat_vertical_cv, ppt_id, 4, 100)
                with open(os.path.join(ppt_id, "problems.txt"), "w", encoding="utf-8") as f:
                    f.write(str(problems))

            reply = None
            if problems:
                if os.path.exists(os.path.join(ppt_id, "reply.txt")):
                    with open(os.path.join(ppt_id, "reply.txt"), "r", encoding="utf-8") as f:
                        reply = ast.literal_eval(f.read().strip())
                elif self.llm:
                    reply = await asyncio.to_thread(LLMManager().generateAnswer, ppt_id)
                    with open(os.path.join(ppt_id, "reply.txt"), "w", encoding="utf-8") as f:
                        f.write(str(reply))
                if reply is not None:
                    reply_text = "LLM答案列表:"
                    for key in problems_keys:
                        reply_text += "\n" + "-"*20
                        problemType = {1: "单选题", 2: "多选题", 3: "投票题", 4: "填空题", 5: "主观题"}.get(problems[key]['problemType'], "其它题型")
                        reply_text += f"\nPPT: 第{key}页 {problemType} {fmt_num(problems[key].get('score', 0))}分"
                        if reply['best_answer'].get(key):
                            if self.lessonIdDict.get(lessonId, {}).get('presentation', 0) == ppt_id:
                                problemId = next((pid for pid, prob in lesson['problems'].items() if prob.get('index') == key), None)
                                self.lessonIdDict[lessonId]['problems'][problemId]['llm_answer'] = reply['best_answer'][key]
                            reply_text += f"\n最佳答案: {reply['best_answer'][key]}\n所有答案:"
                            for r in reply["result"]:
                                if r["answer_dict"].get(key):
                                    reply_text += f"\n[{r['score']}, {r['usedTime']}] {r['name']}: {r['answer_dict'][key]}"
                        else:
                            reply_text += f"\n无答案"
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"{lesson['header']}\n消息: {reply_text}")

    def answer(self, lessonId):
        url = f"https://{self.domain}/api/v3/lesson/problem/answer"
        headers = {
            "referer": f"https://{self.domain}/lesson/fullscreen/v3/{lessonId}?source=5",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "cookie": self.cookie,
            "Content-Type": "application/json",
            "Authorization": self.lessonIdDict[lessonId]['Authorization']
        }
        llm_answer = self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']].get('llm_answer')
        tp = self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['problemType']
        problemType = {1: "单选题", 2: "多选题", 3: "投票题", 4: "填空题", 5: "主观题"}.get(tp, "其它题型")
        if llm_answer:
            answer = llm_answer
        else:
            if tp == 1: # 单选题
                answer = [self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['options'][0]['key']]
            elif tp == 2: # 多选题
                answer = [self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['options'][0]['key']]
            elif tp == 3: # 投票题
                answer = [self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['options'][0]['key']]
            elif tp == 4: # 填空题
                answer = [''] * len(self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['blanks'])
            elif tp == 5: # 主观题
                answer = ['']
            else: # 其它题型
                answer = ['']
        data = {
            "dt": int(time.time()*1000),
            "problemId": self.lessonIdDict[lessonId]['problemId'],
            "problemType": tp,
            "result": answer if tp != 5 else {"content": answer[0], "pics": [{"pic": "", "thumb": ""}]}
        }
        res = None
        try:
            retries = 3
            while retries > 0:
                res = requests.post(url=url, headers=headers, json=data, timeout=timeout)
                if res.json().get('msg') != 'OK':
                    retries -= 1
                    time.sleep(1)
                else:
                    break
        except:
            pass
        if res is not None:
            self.set_authorization(res, lessonId)
        self.msgmgr.sendMsg(f"{self.lessonIdDict[lessonId]['header']}\nPPT: 第{self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['index']}页 {problemType} {fmt_num(self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']].get('score', 0))}分\n问题: {self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['body']}\n提交答案: {answer}")

    async def ws_controller(self, func, *args, retries=3, delay=10):
        attempt = 0
        while attempt <= retries:
            try:
                await func(*args)
                return  # 如果成功就直接返回
            except:
                attempt += 1
                if attempt <= retries:
                    await asyncio.sleep(delay)
                    print(f"重试 ({attempt}/{retries})")

    async def ws_login(self):
        uri = f"wss://{self.domain}/wsapp/"
        async with websockets.connect(uri, ping_timeout=100, ping_interval=5) as websocket:
            # 发送 "hello" 消息以建立连接
            hello_message = {
                "op": "requestlogin",
                "role": "web",
                "version": 1.4,
                "type": "qrcode",
                "from": "web"
            }
            await websocket.send(json.dumps(hello_message))
            server_response = await recv_json(websocket)
            qrcode_url = server_response['ticket']
            download_qrcode(qrcode_url, self.name)
            await asyncio.to_thread(self.msgmgr.sendImage, "qrcode.jpg")
            server_response = await asyncio.wait_for(recv_json(websocket), timeout=60)
            self.web_login(server_response['UserID'], server_response['Auth'])

    async def ws_lesson(self, lessonId):
        flag_ppt = 1
        flag_si = 1
        def del_dict():
            nonlocal flag_ppt, flag_si
            flag_ppt = 1
            flag_si = 1
            keys_to_remove = ['presentation', 'si', 'unlockedproblem', 'covers', 'problems', 'problemId']
            for key in keys_to_remove:
                if self.lessonIdDict[lessonId].get(key) is not None:
                    del self.lessonIdDict[lessonId][key]
        del_dict()
        uri = f"wss://{self.domain}/wsapp/"
        async with websockets.connect(uri, ping_timeout=60, ping_interval=5) as websocket:
            # 发送 "hello" 消息以建立连接
            hello_message = {
                "op": "hello",
                "userid": self.lessonIdDict[lessonId]['userid'],
                "role": "student",
                "auth": self.lessonIdDict[lessonId]['Auth'],
                "lessonid": lessonId
            }
            await websocket.send(json.dumps(hello_message))
            self.lessonIdDict[lessonId]['websocket'] = websocket
            while True and time.time() - self.lessonIdDict[lessonId]['startTime'] < 36000:
                try:
                    server_response = await recv_json(websocket)
                except:
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 连接断开")
                    break
                op = server_response['op']
                if op in ["hello", "fetchtimeline"]:
                    reversed_timeline = list(reversed(server_response['timeline']))
                    for item in reversed_timeline:
                        if 'pres' in item:
                            if flag_ppt == 0 and self.lessonIdDict[lessonId]['presentation'] != item['pres']:
                                del_dict()
                                await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 课件更新")
                            self.lessonIdDict[lessonId]['presentation'] = item['pres']
                            self.lessonIdDict[lessonId]['header'] = re.sub(r'PPT编号: .*?\n', f"PPT编号: {self.lessonIdDict[lessonId]['presentation']}\n", self.lessonIdDict[lessonId]['header'])
                            self.lessonIdDict[lessonId]['si'] = item['si']
                            break
                    if server_response.get('presentation'):
                        if flag_ppt == 0 and self.lessonIdDict[lessonId]['presentation'] != server_response['presentation']:
                            del_dict()
                            await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 课件更新")
                        self.lessonIdDict[lessonId]['presentation'] = server_response['presentation']
                        self.lessonIdDict[lessonId]['header'] = re.sub(r'PPT编号: .*?\n', f"PPT编号: {self.lessonIdDict[lessonId]['presentation']}\n", self.lessonIdDict[lessonId]['header'])
                    if server_response.get('slideindex'):
                        self.lessonIdDict[lessonId]['si'] = server_response['slideindex']
                    if server_response.get('unlockedproblem'):
                        self.lessonIdDict[lessonId]['unlockedproblem'] = server_response['unlockedproblem']
                elif op in ["showpresentation", "presentationupdated", "presentationcreated", "showfinished"]:
                    if server_response.get('presentation'):
                        if flag_ppt == 0 and self.lessonIdDict[lessonId]['presentation'] != server_response['presentation']:
                            del_dict()
                            await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 课件更新")
                        self.lessonIdDict[lessonId]['presentation'] = server_response['presentation']
                        self.lessonIdDict[lessonId]['header'] = re.sub(r'PPT编号: .*?\n', f"PPT编号: {self.lessonIdDict[lessonId]['presentation']}\n", self.lessonIdDict[lessonId]['header'])
                    if server_response.get('slideindex'):
                        self.lessonIdDict[lessonId]['si'] = server_response['slideindex']
                    if server_response.get('unlockedproblem'):
                        self.lessonIdDict[lessonId]['unlockedproblem'] = server_response['unlockedproblem']
                elif op in ["slidenav"]:
                    if server_response['slide'].get('pres'):
                        if flag_ppt == 0 and self.lessonIdDict[lessonId]['presentation'] != server_response['slide']['pres']:
                            del_dict()
                            await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 课件更新")
                        self.lessonIdDict[lessonId]['presentation'] = server_response['slide']['pres']
                        self.lessonIdDict[lessonId]['header'] = re.sub(r'PPT编号: .*?\n', f"PPT编号: {self.lessonIdDict[lessonId]['presentation']}\n", self.lessonIdDict[lessonId]['header'])
                    if server_response['slide'].get('si'):
                        self.lessonIdDict[lessonId]['si'] = server_response['slide']['si']
                    if server_response.get('unlockedproblem'):
                        self.lessonIdDict[lessonId]['unlockedproblem'] = server_response['unlockedproblem']
                elif op in ["unlockproblem", "extendtime"]:
                    if server_response['problem'].get('pres'):
                        if flag_ppt == 0 and self.lessonIdDict[lessonId]['presentation'] != server_response['problem']['pres']:
                            del_dict()
                            await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 课件更新")
                        self.lessonIdDict[lessonId]['presentation'] = server_response['problem']['pres']
                        self.lessonIdDict[lessonId]['header'] = re.sub(r'PPT编号: .*?\n', f"PPT编号: {self.lessonIdDict[lessonId]['presentation']}\n", self.lessonIdDict[lessonId]['header'])
                    if server_response['problem'].get('si'):
                        self.lessonIdDict[lessonId]['si'] = server_response['problem']['si']
                    if server_response.get('unlockedproblem'):
                        self.lessonIdDict[lessonId]['unlockedproblem'] = server_response['unlockedproblem']
                    self.lessonIdDict[lessonId]['problemId'] = server_response['problem']['prob']
                    problemType = {1: "单选题", 2: "多选题", 3: "投票题", 4: "填空题", 5: "主观题"}.get(self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['problemType'], "其它题型")
                    text_result = f"PPT: 第{self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['index']}页 {problemType} {fmt_num(self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']].get('score', 0))}分\n问题: {self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['body']}"
                    answer = self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']].get('llm_answer', [])
                    if 'options' in self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]:
                        for option in self.lessonIdDict[lessonId]['problems'][self.lessonIdDict[lessonId]['problemId']]['options']:
                            text_result += f"\n- {option['key']}: {option['value']}"
                    if answer not in [[], None, 'null']:
                        answer_text = ', '.join(answer)
                        text_result += f"\n答案: {answer_text}"
                    else:
                        text_result += "\n答案: 暂无"
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n解锁问题:\n{text_result}")
                    if self.an:
                        await asyncio.sleep(randint(5, 10))
                        await asyncio.to_thread(self.answer, lessonId)
                elif op in ["lessonfinished"]:
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 下课了")
                    break
                if flag_ppt == 1 and self.lessonIdDict[lessonId].get('presentation') is not None:
                    flag_ppt = 0
                    asyncio.create_task(self.fetch_presentation(lessonId))
                if flag_si == 1 and self.lessonIdDict[lessonId].get('si') is not None and self.lessonIdDict[lessonId].get('covers') is not None and self.lessonIdDict[lessonId]['si'] in self.lessonIdDict[lessonId]['covers']:
                    await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 正在播放PPT第{self.lessonIdDict[lessonId]['si']}页")
                    if self.si:
                        del self.lessonIdDict[lessonId]['si']
                    else:
                        flag_si = 0
            await asyncio.to_thread(self.msgmgr.sendMsg, f"{self.lessonIdDict[lessonId]['header']}\n消息: 连接关闭")
            del self.lessonIdDict[lessonId]

    async def lesson_attend(self):
        if not self.lessonIdNewList:
            return
        coros = [self.ws_lesson(lessonId) for lessonId in self.lessonIdNewList]
        self.lessonIdNewList = []
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                print(f"ws_lesson 任务异常: {r}")

async def _handle_ykt_once(ykt):
    await ykt.get_cookie()
    await asyncio.to_thread(ykt.join_classroom)
    got, to_close_ids = await asyncio.to_thread(ykt.get_lesson)
    if got:
        await asyncio.to_thread(ykt.lesson_checkin)

    for lessonId in to_close_ids:
        ws = ykt.lessonIdDict.get(lessonId, {}).get('websocket')
        if ws is not None:
            try:
                await ws.close()
            except Exception as e:
                print(f"关闭 websocket 失败: {e}")
        ykt.lessonIdDict.pop(lessonId, None)

    await ykt.lesson_attend()

async def ykt_users():
    ykts = [yuketang(user) for user in users if user['enabled']]
    while True:
        await asyncio.gather(*(_handle_ykt_once(ykt) for ykt in ykts), return_exceptions=True)
        await asyncio.sleep(30)
