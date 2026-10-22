"""Synthetic Greenhouse controls, including React-select-style dropdowns and uploads."""

import html
import json
from fastapi import Request
from fastapi.responses import HTMLResponse, Response, JSONResponse

from .forms import greenhouse_form
from .workflow import Workflow
from .greenhouse_transport import approved_upload, validate_application


def definition(scenario=None):
    fields = [
        ("first_name", "First Name", "input_text", True),
        ("last_name", "Last Name", "input_text", True),
        ("preferred_name", "Preferred First Name", "input_text", False),
        ("email", "Email", "input_text", True),
        ("phone", "Phone", "input_text", False),
        ("resume", "Resume/CV", "input_file", True),
        (
            "question_101",
            "Are you eligible to work in the U.S.?",
            "multi_value_single_select",
            True,
        ),
        (
            "question_102",
            "Do you require work authorization?",
            "multi_value_single_select",
            True,
        ),
        ("question_103", "What interests you about this role?", "textarea", True),
        ("question_104", "LinkedIn Profile", "input_text", False),
        ("question_105", "Who referred you?", "input_text", False),
    ]
    data = {
        "education": "education_optional",
        "data_compliance": [{"requires_processing_consent": False}],
        "questions": [
            {
                "label": label,
                "required": required,
                "fields": [
                    {
                        "name": name,
                        "type": kind,
                        "values": [
                            {"label": "Yes", "value": 1},
                            {"label": "No", "value": 0},
                        ]
                        if kind == "multi_value_single_select"
                        else [],
                    }
                ],
            }
            for name, label, kind, required in fields
        ],
    }

    if scenario == "greenhouse_demographics":
        data.update(
            {
                "company_name": "Example Greenhouse",
                "compliance": [
                    {
                        "type": "eeoc",
                        "questions": [
                            {
                                "label": label,
                                "required": False,
                                "fields": [
                                    {
                                        "name": name,
                                        "type": "multi_value_single_select",
                                        "values": [
                                            {"label": label, "value": oid}
                                            for label, oid in options
                                        ],
                                    }
                                ],
                            }
                            for name, label, options in [
                                (
                                    "gender",
                                    "Gender",
                                    [
                                        ("Decline To Self Identify", "3"),
                                        ("Female", "2"),
                                        ("Male", "1"),
                                    ],
                                ),
                                (
                                    "race",
                                    "Race",
                                    [
                                        ("Decline To Self Identify", "8"),
                                        ("White", "5"),
                                        ("Hispanic or Latino", "4"),
                                        ("Asian", "2"),
                                    ],
                                ),
                                (
                                    "veteran_status",
                                    "VeteranStatus",
                                    [
                                        ("I don't wish to answer", "3"),
                                        ("I am not a protected veteran", "1"),
                                    ],
                                ),
                            ]
                        ],
                    }
                ],
                "demographic_questions": {
                    "description": "Optional synthetic survey.",
                    "questions": [
                        {
                            "id": 8001,
                            "label": "How would you describe your gender identity?",
                            "type": "multi_value_multi_select",
                            "required": False,
                            "answer_options": [
                                {"label": label, "id": oid, "free_form": free}
                                for label, oid, free in [
                                    ("Man", 9001, False),
                                    ("Non-binary", 9002, False),
                                    ("Woman", 9003, False),
                                    ("I prefer to self-describe", 9004, True),
                                    ("I don't wish to answer", 9005, False),
                                ]
                            ],
                        },
                        {
                            "id": 8002,
                            "label": "Do you identify as transgender?",
                            "type": "multi_value_single_select",
                            "required": False,
                            "answer_options": [
                                {"label": "Yes", "id": 9010},
                                {"label": "No", "id": 9011},
                                {"label": "I don't wish to answer", "id": 9012},
                            ],
                        },
                    ],
                },
                "data_compliance": [{"demographic_data_consent_applies": True}],
            }
        )
    return data


def register_greenhouse_fixture(api, db):
    flow = Workflow(db)

    def candidate(aid):
        app = flow.get(aid)
        if app["candidate"] != "fixture":
            raise ValueError("Synthetic applications only")
        return app

    def metadata(aid):
        return {
            "url": db.settings.origin + "/fixture/greenhouse-upload",
            "resume": {"fields": {}, "key": aid + "/resume"},
        }

    @api.get("/fixture/greenhouse-presign")
    def presign(application_id: str):
        candidate(application_id)
        return metadata(application_id)

    @api.post("/fixture/greenhouse-upload")
    async def upload(request: Request):
        from email.parser import BytesParser
        from email import policy

        body = await request.body()
        content_type = request.headers.get("content-type", "")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        )
        key = next(
            (
                p.get_payload(decode=True).decode()
                for p in message.iter_parts()
                if p.get_param("name", header="content-disposition") == "key"
            ),
            "",
        )
        aid = key.split("/")[0]
        app = candidate(aid)
        review = flow.review(aid)
        attempt = db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if (
            not attempt
            or attempt["payload_hash"] != review["hash"]
            or app["state"] not in ("submission_attempt", "receipt_verification")
        ):
            raise ValueError("Matching approved attempt required before uploading")
        _, url, _, _ = approved_upload(
            db, body, content_type, review["payload"], metadata(aid)
        )
        db.audit("fixture.greenhouse_uploaded", aid, {"file_url": url})
        return Response(status_code=204)

    @api.post("/fixture/greenhouse/{aid}")
    async def receive(aid: str, request: Request):
        app = candidate(aid)
        review = flow.review(aid)
        attempt = db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if (
            not attempt
            or attempt["payload_hash"] != review["hash"]
            or app["state"] not in ("submission_attempt", "receipt_verification")
        ):
            raise ValueError("Matching approved attempt required before sending")
        record = db.one(
            "SELECT detail FROM audit WHERE subject=%s AND event='fixture.greenhouse_uploaded'",
            (aid,),
        )
        if not record:
            raise ValueError("The reviewed resume was not uploaded")
        from .forms import FormSpec

        spec = FormSpec.model_validate(app["payload"]["form_plan"]["spec"])
        options = {
            f["name"]: {v["label"]: v["value"] for v in f.get("values", [])}
            for q in definition(app["payload"].get("scenario"))["questions"]
            for f in q["fields"]
        }
        if not validate_application(
            await request.body(),
            spec,
            review["payload"],
            options,
            {"resume": {**record["detail"], "confirmed": True}},
        ):
            raise ValueError("The outgoing answers do not match approval")
        if app["payload"].get("scenario") == "greenhouse_blocked_verification":
            return JSONResponse({"code": "captcha-failed"}, status_code=428)
        if app["payload"].get("scenario") == "greenhouse_email_challenge":
            envelope = await request.json()
            if envelope.get("security_code") != "Ab12Cd34":
                return JSONResponse(
                    {
                        "code": "invalid-security-code"
                        if envelope.get("security_code")
                        else "captcha-failed",
                        "security_code_recipient": "alex@example.test",
                    },
                    status_code=428,
                )
        db.audit("fixture.greenhouse_received", aid)
        return {"ok": True}

    @api.get("/fixture/greenhouse/{aid}/confirmation", response_class=HTMLResponse)
    def receipt(aid: str):
        app = candidate(aid)
        if not db.one(
            "SELECT 1 FROM audit WHERE subject=%s AND event='fixture.greenhouse_received'",
            (aid,),
        ):
            raise ValueError("No application was received")
        text = (
            "Processing"
            if app["payload"].get("scenario") == "greenhouse_no_receipt"
            else "Thank you for applying!"
        )
        return HTMLResponse("<h1>" + text + "</h1>")

    @api.get("/fixture/greenhouse/{aid}", response_class=HTMLResponse)
    def form(aid: str):
        app = candidate(aid)
        spec = greenhouse_form(
            definition(app["payload"].get("scenario")), "", app["requisition"]
        )
        controls = []
        for f in spec.fields:
            required = f' aria-required="{str(f.required).lower()}"'
            label = f'<label for="{f.name}">{html.escape(f.label)}</label>'
            metadata = (
                ' data-field="'
                + html.escape(json.dumps(f.model_dump()), quote=True)
                + '"'
            )
            if f.type in ("select", "multiselect"):
                control = f'''<div class="select__value-container"><span class="select__single-value"></span>
                <input id="{f.name}" role="combobox"{required} data-options='{html.escape(json.dumps(f.options), quote=True)}'{metadata} readonly></div>'''
            elif f.binding == "data_compliance":
                label = f'<label for="{f.name}_1">{html.escape(f.label)}</label>'
                control = f'<input id="{f.name}_1" type="checkbox"{required}{metadata}>'
            elif f.type == "textarea":
                control = f'<textarea id="{f.name}"{required}></textarea>'
            else:
                control = f'<input id="{f.name}" type="{"file" if f.type == "file" else "text"}"{required}>'
            if f.binding == "eeoc" and f.name == "race":
                control = f'<div id="race-details" hidden>{label}{control}</div>'
                label = (
                    '<label for="hispanic_ethnicity">Are you Hispanic/Latino?</label>'
                )
                control = (
                    """<div class="select__value-container"><span class="select__single-value"></span><input id="hispanic_ethnicity" role="combobox" data-options='["Yes","No","Decline To Self Identify"]' readonly></div>"""
                    + control
                )
            attrs = (
                f' role="group" aria-labelledby="upload-label-{f.name}"'
                if f.type == "file"
                else ""
            )
            controls.append(f'<div class="field-wrapper"{attrs}>{label}{control}</div>')
        scenario = app["payload"].get("scenario")
        challenge = (
            "<p data-challenge>Complete employer verification</p>"
            if scenario == "greenhouse_challenge"
            else ""
        )
        changed = (
            '<label for="extra">New required credential</label><input id="extra" required>'
            if scenario == "greenhouse_changed"
            else ""
        )
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
        <title>Example Greenhouse application</title>
        <h1>Example employer</h1><p>Synthetic form. No real application is sent.</p>{challenge}
        <form id="application-form" data-delayed="{str(scenario == "greenhouse_delayed").lower()}" data-extra-history="{str(scenario == "greenhouse_extra_history").lower()}">{"".join(controls)}{changed}
        <button type="submit">Submit application</button></form>
        <script src="/fixture/greenhouse.js"></script>""")

    @api.get("/fixture/greenhouse.js")
    def script():
        return Response(
            """
        const form=document.querySelector('form');
        window.uploads={};
        const metadata=fetch('/fixture/greenhouse-presign?application_id='+location.pathname.split('/').pop()).then(r=>r.json());
        document.querySelectorAll('[role=combobox]').forEach(input=>{
            input.addEventListener('click',async()=>{
                if(form.dataset.delayed==='true' && !input.dataset.ready){
                    const response=await fetch('/fixture/greenhouse.js');
                    if(!response.ok)return;
                    await new Promise(resolve=>setTimeout(resolve,100));
                    input.dataset.ready='1';
                }
                document.querySelectorAll('[role=option]').forEach(e=>e.remove());
                for(const text of JSON.parse(input.dataset.options)){
                    const option=document.createElement('div');
                    option.setAttribute('role','option');option.textContent=text;
                    option.onclick=()=>{
                        const field=JSON.parse(input.dataset.field || '{}');
                        if(field.type==='multiselect'){
                            if(![...input.parentElement.querySelectorAll('.select__multi-value__label')].some(e=>e.textContent===text)){
                                const chip=document.createElement('span');chip.className='select__multi-value__label';chip.textContent=text;input.parentElement.append(chip);
                            }
                        }else input.parentElement.querySelector('.select__single-value').textContent=text;
                        if(field.free_form_options?.includes(text) && !document.getElementById(input.id+'-freeform')){
                            const extra=document.createElement('input');extra.id=input.id+'-freeform';input.parentElement.append(extra);
                        }
                        if(input.id==='hispanic_ethnicity'){
                            document.querySelector('#race-details').hidden=text!=='No';
                            const race=document.querySelector('#race');
                            race.dataset.options=JSON.stringify(Object.entries(JSON.parse(race.dataset.field).option_ids).filter(([k,v])=>v!=='4').map(([k])=>k));
                        }
                        document.querySelectorAll('[role=option]').forEach(e=>e.remove());
                    };input.parentElement.append(option);
                }
            });
            input.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('[role=option]').forEach(e=>e.remove())});
        });
        document.querySelector('#email').addEventListener('input',e=>{
            fetch('/fixture/should-never-receive-personal-data?email='+encodeURIComponent(e.target.value)).catch(()=>{});
            fetch('/fixture/autosave',{method:'POST',body:e.target.value}).catch(()=>{});
        });
        document.querySelector('#resume').addEventListener('change',async e=>{
            const data=await metadata, file=e.target.files[0], body=new FormData();
            body.append('utf8','✓');body.append('key',data.resume.key);
            body.append('authenticity_token','1234');body.append('Content-Type','application/octet-stream');body.append('file',file);
            const response=await fetch(data.url,{method:'POST',body});
            if(response.ok){
                window.uploads.resume={name:file.name,url:data.url+'/'+data.resume.key};
                const label=document.createElement('div');label.className='file-upload__filename';label.textContent=file.name;
                e.target.parentElement.append(label);e.target.remove();
            }
        });
        form.addEventListener('submit',async e=>{
            e.preventDefault();
            const job={answers_attributes:{},resume_url:window.uploads.resume?.url,resume_url_filename:window.uploads.resume?.name,
                attachments:{},demographic_answers:[],data_compliance:{},
                employments:[{start_date:{month:null,year:null},end_date:{month:null,year:null},current:false}],
                from_job_board_renderer:true};
            if(form.dataset.extraHistory==='true')job.employments[0].company_name='Unreviewed fixture employer';
            let priority=0;
            for(const input of form.querySelectorAll('input,textarea')){
                const id=input.id;if(!id || id==='resume')continue;
                const field=JSON.parse(input.dataset.field || '{}');
                if(id==='hispanic_ethnicity' || id.endsWith('-freeform'))continue;
                if(field.binding==='data_compliance'){job.data_compliance[field.name]=input.checked;continue;}
                if(field.purpose==='self_identification'){
                    let selected=field.type==='multiselect' ? [...input.parentElement.querySelectorAll('.select__multi-value__label')].map(e=>e.textContent) : [input.parentElement.querySelector('.select__single-value').textContent].filter(Boolean);
                    if(field.name==='race'){
                        const parent=document.querySelector('#hispanic_ethnicity').parentElement.querySelector('.select__single-value').textContent;
                        const oid=parent==='Yes'?'4':parent==='Decline To Self Identify'?'8':null;
                        if(oid)selected=[Object.entries(field.option_ids).find(([k,v])=>v===oid)[0]];
                        else if(parent!=='No')selected=[];
                    }
                    if(field.binding==='eeoc'){if(selected.length)job[id]=String(field.option_ids[selected[0]]);}
                    else if(selected.length)job.demographic_answers.push({question_id:Number(field.name),answer_options:selected.map(label=>({answer_option_id:String(field.option_ids[label]),...(field.free_form_options.includes(label)?{text:document.getElementById(id+'-freeform').value}:{})}))});
                    continue;
                }
                if(id.startsWith('question_')){
                    const qid=id.slice(9), answer={question_id:qid,priority:priority++};
                    if(input.getAttribute('role')==='combobox'){
                        answer.boolean_value=input.parentElement.querySelector('.select__single-value').textContent==='Yes'?1:0;
                    }else answer.text_value=input.value;
                    job.answers_attributes[qid]=answer;
                }else job[id]=input.value;
            }
            delete job['security-input-0']; delete job['security-input-1']; delete job['security-input-2']; delete job['security-input-3'];
            delete job['security-input-4']; delete job['security-input-5']; delete job['security-input-6']; delete job['security-input-7'];
            const code=[...form.querySelectorAll('[id^="security-input-"]')].map(e=>e.value).join('');
            const envelope={job_application:job,request_token:'synthetic-native-request-token'};
            if(code)envelope.security_code=code;
            const response=await fetch(location.href,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(envelope)});
            if(response.ok)location.assign(location.href+'/confirmation');
            else if(response.status===428){
                const failure=await response.json();
                if(!failure.security_code_recipient){return;}
                if(!form.querySelector('#security-input-0')){
                    const section=document.createElement('section');
                    section.innerHTML='<h2>Verify your email</h2>'+Array.from({length:8},(_,i)=>
                        '<input type="text" maxlength="1" id="security-input-'+i+'">').join('');
                    form.append(section);
                }
            }
        });
        """,
            media_type="application/javascript",
        )
