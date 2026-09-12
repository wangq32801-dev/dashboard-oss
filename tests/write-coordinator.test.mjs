import assert from 'node:assert/strict';
import {createWriteCoordinator} from '../frontend-modules/write-coordinator.js';

class MemoryStorage {
  constructor(){this.map=new Map();}
  getItem(key){return this.map.has(key)?this.map.get(key):null;}
  setItem(key,value){this.map.set(key,String(value));}
}

const storage=new MemoryStorage();
let calls=[];
let failNetwork=true;
const coordinator=createWriteCoordinator({storage,send:async(route,payload)=>{
  calls.push({route,payload});
  if(failNetwork)throw new Error('offline');
  return {success:true,task:{id:'t1'},receipt:{domain:'tasks',entityId:'t1',verified:true}};
}});

await assert.rejects(()=>coordinator.execute('api/tasks/create',{title:'测试',clientMutationId:'same-key'},{domain:'tasks'}),/offline/);
assert.equal(coordinator.list().length,1);
assert.equal(coordinator.list()[0].status,'pending');
assert.equal(coordinator.list()[0].payload,undefined);

failNetwork=false;
await coordinator.retry(coordinator.list()[0].id);
assert.equal(coordinator.list().length,0);
assert.equal(coordinator.listReceipts().length,1);
assert.equal(calls[0].payload.clientMutationId,calls[1].payload.clientMutationId);

const rejected=createWriteCoordinator({storage:new MemoryStorage(),send:async()=>({success:false,error:'validation'})});
const result=await rejected.execute('api/relations',{id:'r1',clientMutationId:'reject-key'});
assert.equal(result.success,false);
assert.equal(rejected.list()[0].status,'failed');
assert.equal(rejected.list()[0].error,'validation');

console.log('write-coordinator tests passed');
