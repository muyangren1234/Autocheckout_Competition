import queue
import numpy as np
import time
from collections import defaultdict
import json
#from cvs_data_read import csv_file_read as cfr
#from cvs_data_read import ground_truth_read as gtr

from weight_sensor import WeightSensor
from weight_sensor import weight_based_item_estimate

import sys
import logging

from clients import (
    CpsMongoClient,
    CpsApiClient,
    TestCaseClient,
)
from cli import parse_configs
from log import setup_logger


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def main(args=None):
    args = parse_configs(args)
    setup_logger(args.log_level)
    mongo_client = CpsMongoClient(args.db_address)
    api_client = CpsApiClient()
    test_client = TestCaseClient(mongo_client, api_client)
    #test_client.load(f"{args.command}-{args.sample}")
    logger.info(f"Available Test Cases are {test_client.available_test_cases}")
    test_client.set_context(args.command, load=False)
    generate_receipts(test_client)

def load_product_locations(test_client,Weight_sensor_number):
    productList = test_client.list_products()
    out_sensor_product_info = []
    weight_sensor_info = [[] for jj in range(Weight_sensor_number)]
    for aProduct in productList:
        item_info = [(aProduct.product_id.barcode, aProduct.name), aProduct.weight, aProduct.price]
        allFacings = test_client.find_product_facings(aProduct.product_id)
        if len(allFacings) == 0:
            out_sensor_product_info.append(item_info)
            continue
        for aFacing in allFacings:
            for plateLoc in aFacing.plate_ids:
                sensor_number = (plateLoc.gondola_id - 1) * 6 * 12 + (plateLoc.shelf_index- 1) * 12 + plateLoc.plate_index -1
                weight_sensor_info[sensor_number].append(item_info)
    return weight_sensor_info, out_sensor_product_info

def get_sensor_batch(test_client, start_time, batch_length, Weight_sensor_number):
    if start_time <= 0:
        # the first time, we don't know when the timestamps start, so let's find out
        first_data = test_client.find_first_after_time("plate_data",0.0)[0]
        start_time = first_data.timestamp

    batch_data = test_client.find_all_between_time("plate_data", start_time, start_time+batch_length)
    if len(batch_data) == 0:
        return None, -1
    weight_update_data = [np.empty((0,2)) for jj in range(Weight_sensor_number)]
    currentTime = start_time
    for rawData in batch_data:
        currentTime = rawData.timestamp
        startShelf = rawData.plate_id.shelf_index
        startPlate = rawData.plate_id.plate_index
        gondolaId = rawData.plate_id.gondola_id
        dataSize = rawData.data.shape
        nSamples = dataSize[0]
        nShelves = dataSize[1]
        nPlates = dataSize[2]
        ts = np.array(range(nSamples))*(1.0/60) + currentTime # the timestamps in this packet
        ts = ts.reshape((nSamples,1))
        for jj in range(nShelves):
            for kk in range(nPlates):
                weightData = (rawData.data[:,jj,kk]).reshape(nSamples,1)
                if not(np.isnan(weightData).all()):
                    sensor_number = (gondolaId - 1) * 6 * 12 + (startShelf+jj- 1) * 12 + startShelf + kk -1
                    updateData = np.hstack((ts,weightData))
                    prevData = weight_update_data[sensor_number]
                
                    weight_update_data[sensor_number] = np.vstack((prevData, updateData))

    return weight_update_data, currentTime            
    
def generate_receipts(test_client):
    Weight_sensor_number = 360
    
    detected_weight_event_queue = [queue.Queue(0) for kk in
                                   range(Weight_sensor_number)]  # event sotor queue of each sensor
    total_detected_queue = queue.Queue(0)  # number changed_weight timestamp #total queue of detected event
    merged_detected_queue = queue.Queue(0)

    #ground truth data read
    sensor_info, out_info = load_product_locations(test_client, Weight_sensor_number)


    weight_sensor_list = [WeightSensor(jj, {'1':[10,10,2]}, np.array([]), np.array([])) for jj in range(Weight_sensor_number)]
    
    receipts = defaultdict(list)
    buffer_info = []
    pre_timestamp = 0
    pre_system_time = time.time()
    moreData, next_time = get_sensor_batch(test_client, -1, 1.0, Weight_sensor_number)
    while moreData is not None:
        for sensor_number in range(Weight_sensor_number):
            update_data = moreData[sensor_number]
            if update_data.shape[0] == 0:
                continue # no data loaded from the batch
            update_wv = update_data[:,1]
            update_ts = update_data[:,0]
            weight_sensor_list[sensor_number].value_update(total_detected_queue, detected_weight_event_queue, update_wv, update_ts)
        #time.sleep(0.1)

            logger.debug("Detected {} events".format(total_detected_queue.qsize()))

            while not total_detected_queue.empty():
                tmp_info = total_detected_queue.get()
                tmp_timestamp = tmp_info[2]
                
                new_event = False
                if abs(pre_timestamp - tmp_timestamp) > 2:
                    new_event = True
                if new_event:
                    if len(buffer_info) > 0:
                        merged_detected_queue.put(buffer_info)
                        buffer_info = []
                if len(buffer_info) < 1:
                    pre_system_time = time.time()
                buffer_info.append(tmp_info)
                pre_timestamp = tmp_timestamp
                #total_detected_queue.task_done()
                
            now_time = time.time()
            
            if now_time - pre_system_time > 1:
                if len(buffer_info)>0:
                    #print(now_time - pre_system_time)
                    merged_detected_queue.put(buffer_info)
                    buffer_info = []
                pre_system_time = time.time()
                
            while not merged_detected_queue.empty():
                detected_event = merged_detected_queue.get()

                logger.debug(detected_event)
                sensor_number_list =[]
                total_changed_weight = 0
                event_timestamp =0
                for kk in range(len(detected_event)):
                    sub_event = detected_event[kk]
                    sensor_number_list.append(sub_event[0])
                    total_changed_weight = total_changed_weight + sub_event[1]
                    event_timestamp = sub_event[2]
                
                item_fin_name, item_fin_number, item_fin_price = weight_based_item_estimate(sensor_number_list, total_changed_weight, sensor_info, out_info)
                weight_based_item_info =[event_timestamp, item_fin_name, item_fin_number, item_fin_price]
                logger.debug(weight_based_item_info)
                # who is in the store?
                try:
                    target_list = test_client.find_first_after_time("full_targets", event_timestamp)
                except KeyError:
                    logger.error("Could not load targets at time={}".format(event_timestamp))
                else:
                    if target_list is None:
                        logger.error("No targets in database")
                    elif len(target_list) > 0:
                        target_list = target_list[0]
                        logger.debug("There are {} people in the store".format(len(target_list.targets)))
                        chosen = target_list.targets[0].target_id
                        receipts[chosen].append(item_fin_name[0])
                    
                #merged_detected_queue.task_done()
        moreData,next_time = get_sensor_batch(test_client, next_time, 0.5, Weight_sensor_number)
    printout_receipts(test_client, receipts,'BASELINE-1.json')

def printout_receipts(test_client, receipts, receiptFile):
    logger.warn(receipts)
    with open(receiptFile, 'w') as outFile:
        json.dump(receipts, outFile)

if __name__ == "__main__":
    main(sys.argv[1:])
