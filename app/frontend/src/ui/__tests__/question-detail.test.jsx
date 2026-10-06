import {expect, test} from 'bun:test';
import {buildWorkspaceHash, parseWorkspaceHash} from '../../router.js';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';
import {existsSync, mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {validateQuestionDetail} from '../../chat.jsx';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

const id = '00000000-0000-4000-8000-000000000001';
const hash = `#/console?work=ask&request=${id}`;

test('exact question identity survives both route parsers and builders', () => {
  expect(buildWorkspaceHash({view:'console', requestId:id})).toBe(hash);
  expect(buildWorkbenchHash({work:'ask', requestId:id})).toBe(hash);
  expect(parseWorkspaceHash(hash)).toMatchObject({requestId:id, error:null});
  expect(parseWorkbenchRoute(hash)).toMatchObject({work:'ask', requestId:id, error:null});
});

test.each([`&request=${id}`, '&investigation=inv_one', '&artifact=ra_one', '&persona_id=one', '&markets=za', '&topics=za:one', '&work=brief', '&x=%E0%A4%A'])('question route refuses conflicting or malformed selectors %s', suffix => {
  expect(parseWorkspaceHash(hash + suffix).error).toBe('workspace_request_invalid');
  expect(parseWorkbenchRoute(hash + suffix).error).toBe('workspace_request_invalid');
});

/* Restated 2 October 2026: Fieldwork no longer offers a next operation link
   (it is 42's source roster now and links only to Coverage), so the question
   link is checked through the route builders it used. */
test('question links are built locally and refuse supplied selectors', () => {
  expect(buildWorkspaceHash({view:'console', requestId:id})).toBe(hash);
  expect(() => buildWorkbenchHash({work:'ask',requestId:id,markets:['za']})).toThrow();
  expect(() => buildWorkspaceHash({view:'console',requestId:id,markets:['za']})).toThrow();
});

test('stored reply identity, held state and unknown allowance are validated before rendering', () => {
  const value={contract_version:'general_question_detail_v1',request_id:id,observed_state:'partial',question:'Exact question',history:[],selected_market:'za',requested_window:null,response:{answer:'Stored answer',sources:[],intelligence:intelligenceFixture()},window_from_plan:true,reserved_microusd:100000,missing_work:[]};
  expect(validateQuestionDetail(value,id)).toBe(true);
  expect(validateQuestionDetail({...value,observed_state:'held'},id)).toBe(true);
  expect(validateQuestionDetail({...value,observed_state:'unconfirmed',response:null,window_from_plan:false,missing_work:['execution_unconfirmed']},id)).toBe(true);
  for (const changed of [{...value,request_id:'wrong'},{...value,reserved_microusd:null},{...value,reserved_microusd:true},{...value,observed_state:'complete'},{...value,response:null},{...value,missing_work:['private error']},{...value,requested_window:{start:'2026-02-30',end:'2026-03-01'}}]) expect(validateQuestionDetail(changed,id)).toBe(false);
});

test('the stored detail says whether its window came from a plan, and never contradicts itself', () => {
  const value={contract_version:'general_question_detail_v1',request_id:id,observed_state:'partial',question:'Exact question',history:[],selected_market:'za',requested_window:null,response:{answer:'Stored answer',sources:[],intelligence:intelligenceFixture()},window_from_plan:true,reserved_microusd:100000,missing_work:[]};
  const failed={...intelligenceFixture(),status:'unavailable',snapshot_id:null,sections:[],claims:[],receipts:[],readings:[],limitations:['No completed answer is available.'],missing_work:['model_timeout'],review_required:false,ready_for_downstream:false};
  const unplanned={...value,observed_state:'unavailable',response:{answer:'I could not produce a supported answer for this request.',sources:[],error:true,reason:'model_timeout',intelligence:failed},window_from_plan:false};
  expect(validateQuestionDetail(unplanned,id)).toBe(true);
  expect(validateQuestionDetail({...unplanned,window_from_plan:true},id)).toBe(true);
  const {window_from_plan, ...missing}=value;
  for (const changed of [missing,{...value,window_from_plan:null},{...value,window_from_plan:'true'},{...value,window_from_plan:false},{...value,observed_state:'unconfirmed',response:null,window_from_plan:true,missing_work:['execution_unconfirmed']}]) expect(validateQuestionDetail(changed,id)).toBe(false);
});

test('stored complete reply cannot contradict its terminal error flag', () => {
  const value={contract_version:'general_question_detail_v1',request_id:id,observed_state:'complete',question:'Exact question',history:[],selected_market:'za',requested_window:null,response:{answer:'Stored answer',sources:[],error:true,intelligence:{...intelligenceFixture(),status:'complete'}},window_from_plan:true,reserved_microusd:100000,missing_work:[]};
  expect(validateQuestionDetail(value,id)).toBe(false);
});

test.each(['', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA', '%E0%A4%A', 'not-a-request'])('invalid request reference cannot become a normal ask: %s', invalid => {
  const bad='#/console?work=ask&request='+invalid;
  expect(parseWorkspaceHash(bad).error).toBe('workspace_request_invalid');
  expect(parseWorkbenchRoute(bad).error).toBe('workspace_request_invalid');
});

const chrome = [process.env.CHROME_PATH, String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`].filter(Boolean).find(existsSync);
/* Ask redesign, 23 Sept 2026: the follow-up is asked from the "Ask a
   follow-up" field under the stored answer, and sending it hands the question
   to the console with the continued thread, so the probe types and sends one
   and checks the question arrives. The bookkeeping lines now sit in the Request
   and usage details disclosure, still in the page text, and windows read as
   a reader says them. */
for (const [selectedMarket, markets, expectedRegion, requestedMarket, resolvedMarket] of [['za',['za'],'ZA','South Africa','South Africa'], [null,['ke','ng'],'ALL','All markets','Kenya / Nigeria']]) {
test.skipIf(!chrome)('mounted exact request view only reads and preserves market '+expectedRegion, async () => {
  const root = fileURLToPath(new URL('../../../../', import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), 'lp-question-observe-'));
  const entry = `
    import React from 'react';
    import {createRoot} from 'react-dom/client';
    import {flushSync} from 'react-dom';
    import {ChatPage, chatContextReferences} from './frontend/src/chat.jsx';
    import {intelligenceFixture} from './frontend/src/ui/__tests__/fixtures/general-intelligence.js';
    const a='00000000-0000-4000-8000-000000000001', b='00000000-0000-4000-8000-000000000002', c='00000000-0000-4000-8000-000000000003';
    const calls=[];const coverage={contract_version:'general_question_coverage_v1',state:'covered',window:{start:'2026-08-25',end:'2026-09-07'},cutoff_date:'2026-09-07'};const coveragePath='/api/chat/coverage',questionPath='/api/internal/v2/fieldwork/question';const pathOf=url=>new URL(url,'https://example.test').pathname;let lateA,firstA=true,continued=null,continuedMarket=null,continuedQuestion=null;
    const detail=id=>id===c?{contract_version:'general_question_detail_v1',request_id:id,observed_state:'unconfirmed',question:'QUESTION C',history:[],selected_market:'za',requested_window:null,response:null,window_from_plan:false,reserved_microusd:100000,missing_work:['execution_unconfirmed']}:{contract_version:'general_question_detail_v1',request_id:id,observed_state:'complete',question:id===a?'QUESTION A':'QUESTION B',history:[{role:'user',text:'EXACT EARLIER QUESTION'},{role:'assistant',text:'EXACT EARLIER ANSWER'}],selected_market:${JSON.stringify(selectedMarket)},requested_window:{start:'2026-08-01',end:'2026-08-31'},response:{answer:'Stored answer',sources:[],intelligence:{...intelligenceFixture(),status:'complete',request_id:id,resolved_scope:{...intelligenceFixture().resolved_scope,market_scope:${JSON.stringify(markets)}},receipts:intelligenceFixture().receipts.map(row=>({...row,market:${JSON.stringify(markets[0])}}))}},window_from_plan:true,reserved_microusd:100000,missing_work:[]};
    const response=data=>({ok:true,status:200,headers:{get:()=>null},json:()=>Promise.resolve(data)});
    window.fetch=(url,options={})=>{calls.push({url:String(url),method:options.method||'GET'});const requestUrl=new URL(String(url),'https://example.test');if(requestUrl.pathname===coveragePath)return Promise.resolve(response(coverage));if(requestUrl.pathname!==questionPath)return Promise.reject(new Error('Unexpected request path: '+requestUrl.pathname));const selected=requestUrl.searchParams.get('request_id');if(selected===a&&firstA){firstA=false;return new Promise(resolve=>{lateA=()=>resolve(response(detail(a)));});}return Promise.resolve(response(detail(selected)));};
    const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
    const until=async (label,fn)=>{for(let i=0;i<100&&!fn();i++)await wait();if(!fn())throw new Error('DOM condition timed out: '+label);};
    const encode=value=>btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))));
    (async()=>{
      localStorage.removeItem('pulse-chat');const node=document.getElementById('root');const mounted=createRoot(node);
      const show=requestId=>flushSync(()=>mounted.render(React.createElement(ChatPage,{requestId,region:'ZA',embedded:true,onContinueQuestion:(id,market,question)=>{continued=id;continuedMarket=market;continuedQuestion=question;}})));
      show(a);await until('question A GET was sent',()=>calls.some(x=>pathOf(x.url)===questionPath&&new URL(x.url,'https://example.test').searchParams.get('request_id')===a));show(b);await until('question B rendered',()=>node.textContent.includes('QUESTION B'));
      lateA();await wait();const staleAfterLate=node.textContent.includes('QUESTION A');
      const writesBeforeContinue=localStorage.getItem('pulse-chat');show(a);const staleDuringSwitch=node.textContent.includes('QUESTION B');await until('question A rendered after switching back',()=>node.textContent.includes('QUESTION A'));
      show(b);await until('question B rendered after switching back',()=>node.textContent.includes('QUESTION B'));const label=[...node.querySelectorAll('label')].find(x=>x.textContent==='Ask a follow-up');if(!label)throw new Error('Explicit follow-up missing');const field=document.getElementById(label.htmlFor);Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'FOLLOW UP QUESTION');field.dispatchEvent(new Event('input',{bubbles:true}));await wait();const button=[...node.querySelectorAll('button')].find(x=>x.textContent==='Send');if(!button||button.disabled)throw new Error('Follow-up send unavailable');button.click();await wait();
      const saved=JSON.parse(localStorage.getItem('pulse-chat')||'[]');const thread=saved.find(x=>x.id===continued);
      const completeText=node.textContent;show(c);await until('unconfirmed request C rendered',()=>node.textContent.includes('Execution unconfirmed'));const unconfirmedText=node.textContent;
      const coverageCalls=calls.filter(x=>pathOf(x.url)===coveragePath),questionCalls=calls.filter(x=>pathOf(x.url)===questionPath);
      document.getElementById('result').textContent=encode({calls,coverageCalls,questionCalls,questionRequestIds:questionCalls.map(x=>new URL(x.url,'https://example.test').searchParams.get('request_id')),staleAfterLate,staleDuringSwitch,writesBeforeContinue,continued:!!thread,continuedMarket,continuedQuestion,storedMarket:thread?.market,history:thread?.messages.slice(0,3),references:thread?chatContextReferences(thread.messages):null,completeText,unconfirmedText});mounted.unmount();
    })().catch(error=>{document.getElementById('result').textContent=encode({error:String(error.stack||error)});});
  `;
  try {
    await build({stdin:{contents:entry,loader:'jsx',resolveDir:root,sourcefile:'question-detail-mounted.jsx'},bundle:true,format:'iife',jsx:'automatic',platform:'browser',outfile:join(directory,'probe.js'),define:{'process.env.NODE_ENV':'"production"'},nodePaths:[join(root,'frontend','node_modules')],plugins:[{name:'css',setup(builder){builder.onResolve({filter:/\.css$/},()=>({path:'empty',namespace:'empty'}));builder.onLoad({filter:/.*/,namespace:'empty'},()=>({contents:'',loader:'js'}));}}]});
    writeFileSync(join(directory,'probe.html'),'<div id="root"></div><pre id="result">pending</pre><script src="probe.js"></script>');
    const run=spawnSync(chrome,['--headless=new','--disable-gpu','--no-first-run','--virtual-time-budget=10000','--dump-dom',`--user-data-dir=${join(directory,'profile')}`,pathToFileURL(join(directory,'probe.html')).href],{encoding:'utf8',timeout:20000,windowsHide:true});
    const encoded=run.stdout.match(/<pre id="result">([^<]+)<\/pre>/)?.[1];
    const measured=JSON.parse(Buffer.from(encoded||'', 'base64').toString('utf8'));
    expect(measured.error).toBeUndefined();
    expect(measured.staleAfterLate).toBe(false);expect(measured.staleDuringSwitch).toBe(false);
    expect(measured.calls.every(x=>x.method==='GET')).toBe(true);
    expect(measured.calls.every(x=>['/api/chat/coverage','/api/internal/v2/fieldwork/question'].includes(new URL(x.url,'https://example.test').pathname))).toBe(true);
    expect(measured.coverageCalls).toEqual([{url:'/api/chat/coverage',method:'GET'}]);
    expect(measured.questionCalls).toHaveLength(5);expect(measured.questionCalls.every(x=>new URL(x.url,'https://example.test').pathname==='/api/internal/v2/fieldwork/question')).toBe(true);
    expect(measured.questionRequestIds).toEqual(['00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003']);
    expect(measured.writesBeforeContinue).toBeNull();expect(measured.continued).toBe(true);expect(measured.continuedMarket).toBe(expectedRegion);expect(measured.storedMarket).toBe(expectedRegion.toLowerCase());expect(measured.continuedQuestion).toBe('FOLLOW UP QUESTION');
    expect(measured.history.map(x=>x.content)).toEqual(['EXACT EARLIER QUESTION','EXACT EARLIER ANSWER','QUESTION B']);
    expect(measured.references.parent_request_id).toBe('00000000-0000-4000-8000-000000000002');
    expect(measured.completeText).toContain('USD allowance');
    expect(measured.completeText).toContain('Requested market: '+requestedMarket);
    expect(measured.completeText).toContain('Resolved market: '+resolvedMarket);
    expect(measured.completeText).toContain('Requested window: 1 to 31 Aug 2026');
    expect(measured.completeText).toContain('Resolved window: 23 Aug to 5 Sept 2026');
    expect(measured.unconfirmedText).toContain('Requested window: Unspecified');
    expect(measured.unconfirmedText).toContain('Resolved window: Unavailable');
  } finally { rmSync(directory,{recursive:true,force:true}); }
},30000);
}



