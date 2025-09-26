import math
with(open('prime.txt','r+')) as f:
    primeSet = f.read().splitlines()
    primeSetInt = [int(i) for i in primeSet]
    del primeSet

    curNum = primeSetInt[-1]
    print("last number recorded was: " + str(curNum))
    while True:
        curNum += 2
        if curNum % 3 == 0 or curNum % 5 == 0 or math.sqrt(curNum) == math.floor(math.sqrt(curNum)):
            continue
        isPrime = True
        for i in range(3, len(primeSetInt)):
            if i > math.floor(math.sqrt(curNum)):
                break
            if curNum % i == 0:
                isPrime = False
                break
        if isPrime:
            f.write(str(curNum) + '\n')
            primeSetInt.append(curNum)
            print("this number is prime: " + str(curNum))
            print("size of set is now: " + str(len(primeSetInt)))
