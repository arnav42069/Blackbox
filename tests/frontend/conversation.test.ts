import {test} from 'node:test';
import assert from 'node:assert/strict';
import {conversationMessages, executionState, reconcileSession, mergeJournal} from '../../frontend/src/conversation.ts';
const delta=(content:string)=>({type:'answer_delta',message_id:'m',run_id:'r',ts:1,content});
const final={type:'assistant_message',message_id:'m',run_id:'r',ts:1,content:'Hello world',metadata:{state:'complete',conversation_kind:'final'}};

test('stream snapshots replace drafts without token duplication',()=>{
  const result=conversationMessages([],[delta('Hello'),delta('Hello world'),delta('Hello world')]);
  assert.equal(result.length,1);assert.equal(result[0].content,'Hello world');
  assert.equal(result[0].metadata.state,'streaming');
});
test('final event promotes draft in place',()=>{
  const result=conversationMessages([],[delta('Hello'),final]);
  assert.equal(result.length,1);assert.equal(result[0].metadata.state,'complete');
});
test('reload and journal replay preserve persisted final messages',()=>{
  const messages=conversationMessages([],[final]);
  assert.deepEqual(conversationMessages(messages,[delta('Hello'),final]),messages);
});
test('stale replay does not truncate a newer persisted draft',()=>{
  const messages=conversationMessages([],[delta('Hello world')]);
  assert.equal(conversationMessages(messages,[delta('Hello')])[0].content,'Hello world');
});
test('tool activity and assistant output remain distinct',()=>{
  const result=conversationMessages([],[{type:'tool_finished',tool_id:'t',summary:'HTTP 200'},delta('Measured HTTP 200')]);
  assert.equal(result.length,1);assert.equal(result[0].role,'assistant');
});
test('states describe thinking, tools, response and terminal outcomes',()=>{
  assert.equal(executionState('running',[]),'Thinking');
  assert.equal(executionState('running',[{type:'tool_started'}]),'Running tools');
  assert.equal(executionState('running',[{type:'assistant_status',value:'running tool'}]),'Running tools');
  assert.equal(executionState('running',[{type:'assistant_status',value:'generating response'}]),'Generating response');
  for(const [status,label] of [['completed','Completed'],['failed','Failed'],['cancelled','Cancelled']])assert.equal(executionState(status,[]),label);
});

test('a delayed snapshot cannot undo a streamed terminal transition',()=>{
  const snapshot={id:'s',status:'running',messages:[],events:[{event_id:2}]};
  assert.equal(reconcileSession(snapshot,{sid:'s',event_id:3,status:'completed'}).status,'completed');
  assert.equal(reconcileSession({...snapshot,events:[{event_id:4}]},{sid:'s',event_id:3,status:'completed'}).status,'running');
});

test('authoritative newer revisions can shorten content and stale replay cannot restore it',()=>{
  const newer={...delta('Measured result'),revision:2,event_id:3};
  const original={...delta('Unreviewed much longer draft'),revision:1,event_id:2};
  const messages=conversationMessages([],[original,newer]);
  assert.equal(messages[0].content,'Measured result');
  assert.equal(conversationMessages(messages,[original])[0].content,'Measured result');
});

test('user and assistant messages are available from events without a fetched snapshot',()=>{
  const messages=conversationMessages([],[{type:'user_message',message_id:'u',content:'Hello',attachment_ids:['image'],metadata:{},ts:1},delta('Hi')]);
  assert.deepEqual(messages.map(m=>m.role),['user','assistant']);
  assert.equal(messages[0].attachment_ids[0],'image');
});

test('started events do not erase an existing draft or final on replay',()=>{
  const started={type:'assistant_message_started',message_id:'m',content:'',metadata:{state:'streaming',revision:0},ts:1};
  assert.equal(conversationMessages(conversationMessages([],[final]),[started])[0].content,'Hello world');
});


test('delayed resync snapshots preserve newer events within bounded journal size',()=>{
  const live=Array.from({length:2000},(_,i)=>({event_id:i+2,type:'answer_delta'}));
  const merged=mergeJournal(live,[{event_id:1,type:'run_started'},{event_id:2,type:'answer_delta'}]);
  assert.equal(merged.length,2000);
  assert.equal(merged.at(-1).event_id,2001);
  assert.equal(merged[0].event_id,2);
});

test('legacy progress replay cannot demote a persisted final answer',()=>{
  const saved=conversationMessages([],[final]);
  const replay={...final,content:'Earlier draft',metadata:{conversation_kind:'progress',state:'complete'}};
  assert.equal(conversationMessages(saved,[replay])[0].content,'Hello world');
});

test('a terminal event without a final answer creates an explicit visible blocker',()=>{
  const ended={event_id:2,type:'run_failed',run_id:'r',ts:2,message:'Model timeout'};
  const result=conversationMessages([],[ended]);
  assert.equal(result.length,1);
  assert.equal(result[0].role,'assistant');
  assert.match(result[0].content,/Model timeout.*No final assistant response/);
  assert.equal(conversationMessages(result,[ended]).length,1);
});

test('a real persisted final prevents and replaces a synthetic missing-answer blocker',()=>{
  const ended={event_id:2,type:'run_finished',run_id:'r',ts:2,reason:'completed'};
  const completed={...final,metadata:{...final.metadata,run_id:'r'}};
  const blocked=conversationMessages([],[ended]);
  const result=conversationMessages(blocked,[completed,ended]);
  assert.equal(result.length,1);
  assert.equal(result[0].content,'Hello world');
});
